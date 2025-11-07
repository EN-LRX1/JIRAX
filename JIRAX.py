import requests
from requests.auth import HTTPBasicAuth
import json
import os
import csv
from dotenv import load_dotenv
from langchain_ollama import ChatOllama
from langchain_core.prompts import PromptTemplate
from templates import PAYLOAD_GENERATION_TEMPLATE_V4
from fetcher_sql import fetch_and_save_issues
import re

# --- CONFIGURACIÓN ---
load_dotenv()
JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
EMAIL = os.getenv("EMAIL")
API_TOKEN = os.getenv("API_TOKEN")
OUTPUT_FILE = os.getenv("OUTPUT_FILE", "ucm_issues.csv")
JIRA_VERIFY = os.getenv("JIRA_VERIFY", "true").lower() not in ("0", "false", "no")

# --- CARGA ÚNICA DEL LLM Y PROMPT ---
LLM = ChatOllama(model="gpt-oss:latest", temperature=0)
PAYLOAD_PROMPT = PromptTemplate.from_template(PAYLOAD_GENERATION_TEMPLATE_V4)

def load_csv_to_memory():
    """Carga el CSV en memoria al inicio para evitar lecturas repetidas."""
    if not os.path.exists(OUTPUT_FILE):
        return {}
    data = {}
    with open(OUTPUT_FILE, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = row.get("key")
            if key and key.strip():
                data[key.strip().upper()] = row
    return data

# Initialize CSV_DATA globally
CSV_DATA = load_csv_to_memory()

# --- FUNCIONES AUXILIARES ---
def update_jira_issue_api(issue_key: str, update_payload_str: str) -> str:
    """Updates a Jira issue using the REST API."""
    if issue_key not in ("UCM-62", "UCM-64"):
        return f"Permission Error: This test agent is only allowed to modify issues UCM-62 and UCM-64. Attempt to modify {issue_key} was denied."
    if not all([JIRA_DOMAIN, EMAIL, API_TOKEN]):
        return "Error: Missing Jira credentials (JIRA_DOMAIN, EMAIL, API_TOKEN)."

    url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/{issue_key}"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    try:
        payload_dict = json.loads(update_payload_str)
        fields_to_update = payload_dict.get("fields", {})

        if "customfield_10193" in fields_to_update and isinstance(fields_to_update["customfield_10193"], str):
            text_content = fields_to_update["customfield_10193"]
            fields_to_update["customfield_10193"] = {
                "version": 1, "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": text_content}]}]
            }

        variables_prohibidas = [
            "key", "status", "assignee", "customfield_10190", "customfield_10191",
            "customfield_10192", "customfield_10196", "customfield_10194",
            "customfield_10222", "customfield_10213", "customfield_10248"
        ]
        for k in variables_prohibidas:
            fields_to_update.pop(k, None)
        
        if not fields_to_update:
            return f"Skipped: No valid fields to update for issue {issue_key} after filtering."

        jira_payload = {"fields": fields_to_update}
        print(f"SENDING: PUT {url} with Payload: {json.dumps(jira_payload, indent=2)}")
        
        response = requests.put(
            url, data=json.dumps(jira_payload), headers=headers,
            auth=HTTPBasicAuth(EMAIL, API_TOKEN), verify=False
        )
        
        if response.status_code == 204:
            return f"Success: Issue {issue_key} was updated correctly."
        else:
            return f"Error updating issue {issue_key}. Code: {response.status_code}. Response: {response.text}"
            
    except json.JSONDecodeError:
        return f"Error: The 'update_payload_str' is not a valid JSON string. Content: {update_payload_str}"
    except Exception as e:
        return f"Unexpected error while updating the issue: {e}"

def extract_all_issue_keys(text: str) -> list[str]:
    """
    Extrae todas las claves de issue del texto.
    Si no se encuentra ninguna, devuelve TODAS las claves del CSV cargado.
    """
    matches = re.findall(r'([A-Z]{2,}-\d+)', text.upper())
    if not matches:
        print("No specific issue keys found in instruction, targeting ALL issues from CSV.")
        return list(CSV_DATA.keys())
    return list(set(matches))

# --- FLUJO PRINCIPAL ---
def correction_flow(user_instruction: str):
    print("\n--- 0. Updating local data from Jira... ---")
    fetch_and_save_issues()
    global CSV_DATA
    CSV_DATA = load_csv_to_memory()
    
    all_issue_keys = extract_all_issue_keys(user_instruction)
    
    if not all_issue_keys:
        return "❌ Error: No issue keys found in your instruction or in the local data."
    
    print(f"\n--- 1. Targetting {len(all_issue_keys)} issue(s) for processing ---")

    processed_count = 0
    final_results = []

    # --- Procesar cada issue individualmente ---
    for issue_key in all_issue_keys:
        print(f"\n--- Analyzing Issue: {issue_key} ---")

        if issue_key not in CSV_DATA:
            print(f"⚠️ Warning: Issue {issue_key} not found in the loaded data. Skipping.")
            final_results.append(f"{issue_key}: Skipped (not found in local data).")
            continue

        try:
            # 2. Preparamos los datos de UN SOLO issue para el LLM
            issue_data_str = json.dumps(CSV_DATA[issue_key], ensure_ascii=False, indent=2)
            
            # 3. Llamamos al LLM con el prompt y los datos del issue actual
            print(f"Asking LLM for a plan for {issue_key}...")
            chain = PAYLOAD_PROMPT | LLM
            llm_response_content = chain.invoke({
                "all_issues_data": f"**ISSUE DATA ({issue_key}):**\n{issue_data_str}",
                "issue_keys": issue_key, # Generamos payload de uno en uno
                "user_instruction": user_instruction 
            }).content
            
            print(f"LLM Response for {issue_key} (raw):\n{llm_response_content}")

            if llm_response_content.strip().upper() == "NO_UPDATE":
                print(f"✅ LLM determined no update is needed for {issue_key}.")
                final_results.append(f"{issue_key}: No update required.")
                continue

            # 4. Extraemos el payload de la respuesta del LLM
            match = re.search(r'```json\s*(\{.*?\})\s*```', llm_response_content, re.DOTALL)
            
            if not match:
                print(f"⚠️ No valid JSON payload found in LLM response for {issue_key}.")
                final_results.append(f"{issue_key}: Skipped (no payload generated).")
                continue

            json_str = match.group(1)
            parsed_payload = json.loads(json_str)
            final_payload_str = json.dumps({"fields": parsed_payload}, ensure_ascii=False)
            
            # 5. Llamada a la API
            result_message = update_jira_issue_api(issue_key, final_payload_str)
            
            if "Success" in result_message:
                print(f"✅ SUCCESS: Issue {issue_key} was updated.")
                processed_count += 1
            else:
                print(f"❌ FAILURE: The update for {issue_key} failed. {result_message}")
            
            final_results.append(f"{issue_key}: {result_message}")

        except json.JSONDecodeError as e:
            print(f"FAILURE: Invalid JSON for {issue_key}: {e}")
            final_results.append(f"Skipped {issue_key}: Invalid JSON syntax.")
            continue
        except Exception as e:
            print(f"FAILURE: An unexpected error occurred while processing {issue_key}: {e}")
            final_results.append(f"Skipped {issue_key}: Unexpected error ({e}).")
            continue
    
    summary = f"✅ PROCESS COMPLETE: {processed_count} of {len(all_issue_keys)} targeted issues were successfully updated."
    return f"{summary}\n\nDetails:\n- " + "\n- ".join(final_results)

if __name__ == "__main__":
    print("Jira Autonomous Agent Initialized. Type 'salir' or synonyms to quit.")
    
    while True:
        user_input = input("\n> What task would you like to perform?: ")
        if user_input.lower() in ['exit', 'quit', 'salir', 'end', 'amaitu']:
            break
        final_result = correction_flow(user_input)
        print("\n--- TASK COMPLETE ---")
        print(final_result)