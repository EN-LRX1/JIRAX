import requests
from requests.auth import HTTPBasicAuth
import json
import os
import csv
from dotenv import load_dotenv
import re
from langchain_ollama import ChatOllama
from langchain_core.prompts import PromptTemplate
from templates import PAYLOAD_GENERATION_TEMPLATE_V5 as PAYLOAD_GENERATION_TEMPLATE
from fetcher_sql import fetch_and_save_issues
load_dotenv()
JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
EMAIL = os.getenv("EMAIL")
API_TOKEN = os.getenv("API_TOKEN")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-oss:latest")
OUTPUT_FILE = os.getenv("OUTPUT_FILE", "ucm_issues.csv")
LLM = ChatOllama(model=LLM_MODEL, temperature=0)
PAYLOAD_PROMPT = PromptTemplate.from_template(PAYLOAD_GENERATION_TEMPLATE)

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

def update_jira_issue_api(issue_key: str, update_payload_str: str) -> str:
    """Updates a Jira issue using the REST API v3."""
    if issue_key not in ("UCM-62", "UCM-64"):
        return f"Permission Error: Test agent can only modify UCM-62 and UCM-64."

    if not all([JIRA_DOMAIN, EMAIL, API_TOKEN]):
        return "Error: Missing Jira credentials."

    url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/{issue_key}"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    
    try:
        payload_dict = json.loads(update_payload_str)
        fields_to_update = (payload_dict.get("fields") or {}).copy()

        prohibited_fields = [
            "key", "status", "assignee", "customfield_10190", "customfield_10191",
            "customfield_10192", "customfield_10196", "customfield_10194",
            "customfield_10222", "customfield_10213", "customfield_10248"
        ]
        for k in prohibited_fields:
            fields_to_update.pop(k, None)

        if not fields_to_update:
            return f"Skipped: No valid fields to update for {issue_key} after filtering."

        jira_payload = {"fields": fields_to_update}
        response = requests.put(
            url, headers=headers, data=json.dumps(jira_payload),
            auth=HTTPBasicAuth(EMAIL, API_TOKEN), verify=False, timeout=30
        )
        if response.status_code == 204:
            return f"Success: Issue {issue_key} was updated correctly."
        return f"Error updating {issue_key}. Code: {response.status_code}. Response: {response.text}"
    except Exception as e:
        return f"An exception occurred: {e}"

def extract_all_issue_keys(text: str, all_known_keys: list) -> list[str]:
    """Extrae todas las claves de issue del texto. Si no hay, devuelve todas las del CSV."""
    matches = re.findall(r'([A-Z]{2,}-\d+)', text.upper())
    if not matches:
        print("No specific issue keys found, targeting ALL issues from CSV.")
        return sorted(all_known_keys)
    return sorted(list(set(matches)))

def correction_flow(user_instruction: str):
    print("\n--- 0. Updating local data from Jira... ---")
    fetch_and_save_issues()
    all_issues_data = load_csv_to_memory()
    
    if not all_issues_data:
        return "❌ Error: No issue keys found to process."
    
    all_issue_keys = extract_all_issue_keys(user_instruction, list(all_issues_data.keys()))
    
    print(f"\n--- 1. Targetting {len(all_issue_keys)} issue(s) for processing ---")

    final_results = []
    processed_issues_memory = {}

    for issue_key in all_issue_keys:
        print(f"\n--- Analyzing Issue: {issue_key} ---")
        current_issue = all_issues_data.get(issue_key)
        if not current_issue:
            final_results.append(f"{issue_key}: Skipped (not found).")
            continue

        try:
            # --- CONTEXTO ---
            memory_context = "\n".join([
                f"- {key}:\n  Summary: {data.get('summary', 'N/A')}\n  Description: {data.get('customfield_10193', 'N/A')}"
                for key, data in processed_issues_memory.items()
            ])
            if not memory_context:
                memory_context = "No issues processed yet in this session."

            print(f"Asking LLM for a plan for {issue_key}...")
            chain = PAYLOAD_PROMPT | LLM
            llm_response_content = chain.invoke({
                "current_issue_data": json.dumps(current_issue, indent=2),
                "user_instruction": user_instruction,
                "current_issue_key": issue_key,
                "processed_issues_context": memory_context 
            }).content
            
            print(f"LLM Response for {issue_key} (raw):\n{llm_response_content}")

            processed_issues_memory[issue_key] = current_issue

            if llm_response_content.strip().upper() == "NO_UPDATE":
                final_results.append(f"{issue_key}: No update required.")
                continue

            match = re.search(r'```json\s*(\{.*?\})\s*```', llm_response_content, re.DOTALL)
            if not match:
                final_results.append(f"{issue_key}: Skipped (no payload generated).")
                continue

            final_payload_str = json.dumps({"fields": json.loads(match.group(1))})
            result_message = update_jira_issue_api(issue_key, final_payload_str)
            final_results.append(f"{issue_key}: {result_message}")

        except Exception as e:
            print(f"FATAL: An unexpected error occurred while processing {issue_key}: {e}")
            final_results.append(f"Skipped {issue_key}: Unexpected error ({e}).")
            continue
    
    summary = f"✅ PROCESS COMPLETE: {len(final_results)} of {len(all_issue_keys)} targeted issues were processed."
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
