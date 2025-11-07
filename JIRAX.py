import requests
from requests.auth import HTTPBasicAuth
import json
import os
import csv
from dotenv import load_dotenv
from langchain_ollama import ChatOllama
from langchain_core.prompts import PromptTemplate
from langchain_core.tools import tool
from templates import PAYLOAD_GENERATION_TEMPLATE
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
PAYLOAD_PROMPT = PromptTemplate.from_template(PAYLOAD_GENERATION_TEMPLATE)

def load_csv_to_memory():
    """Carga el CSV en memoria al inicio para evitar lecturas repetidas."""
    if not os.path.exists(OUTPUT_FILE):
        return {}
    
    data = {}
    with open(OUTPUT_FILE, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        orig_fieldnames = reader.fieldnames or []
        norm_map = {fn: fn.strip().lower() for fn in orig_fieldnames}
        key_columns = [orig for orig, norm in norm_map.items() if norm == "key" or ("issue" in norm and "key" in norm)]
        if not key_columns:
            key_columns = [orig for orig, norm in norm_map.items() if "key" in norm or norm == "id"]

        for row in reader:
            for kc in key_columns:
                val = row.get(kc)
                if val and val.strip():
                    data[val.strip().upper()] = row
                    break
    return data

# --- FUNCIONES AUXILIARES ---
def update_jira_issue_api(issue_key: str, update_payload_str: str) -> str:
    """
    Updates a Jira issue using the REST API.
    """
    if issue_key != "UCM-62":
        return f"Permission Error: This test agent is only allowed to modify issue UCM-62. Attempt to modify {issue_key} was denied."
    if not all([JIRA_DOMAIN, EMAIL, API_TOKEN]):
        return "Error: Missing Jira credentials (JIRA_DOMAIN, EMAIL, API_TOKEN)."

    url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/{issue_key}"
    headers = {"Accept":"application/json","Content-Type":"application/json"}
    try:
        payload_dict = json.loads(update_payload_str)
        
        fields_to_update = {}
        if "actions" in payload_dict and payload_dict["actions"]:
            fields_to_update = payload_dict["actions"][0].get("fields", {})
        elif "fields" in payload_dict:
            fields_to_update = payload_dict["fields"]
        else:
            return f"Error: No 'fields' found in the payload to update for issue {issue_key}."

        # Convertir campos de texto enriquecido a ADF
        if "customfield_10193" in fields_to_update and isinstance(fields_to_update["customfield_10193"], str):
            text_content = fields_to_update["customfield_10193"]
            fields_to_update["customfield_10193"] = {
                "version": 1,
                "type": "doc",
                "content": [{
                    "type": "paragraph",
                    "content": [{
                        "type": "text",
                        "text": text_content
                    }]
                }]
            }

        jira_payload = {"fields": fields_to_update}
        
        print(f"ENVIANDO: PUT {url} con Payload: {json.dumps(jira_payload, indent=2)}")
        
        # --- LLAMADA API ---
        response = requests.put(
            url,
            data=json.dumps(jira_payload),
            headers=headers,
            auth=HTTPBasicAuth(EMAIL, API_TOKEN),
            verify=False
        )
        
        if response.status_code == 204:
            return f"Success: Issue {issue_key} was updated correctly."
        else:
            return f"Error updating issue {issue_key}. Code: {response.status_code}. Response: {response.text}"
            
    except json.JSONDecodeError:
        return f"Error: The 'update_payload_str' is not a valid JSON string. Content: {update_payload_str}"
    except Exception as e:
        return f"Unexpected error while updating the issue: {e}"

def extract_issue_key(text: str) -> str | None:
    """Usa regex para encontrar un patrón de clave de Jira (p.ej., UCM-62, PROJ-1234) en un texto."""
    match = re.search(r'([A-Z]{2,}-\d+)', text.upper())
    if match:
        return match.group(1)
    return None

# --- HERRAMIENTAS ---
@tool
def query_jira_database(query: str) -> str:
    """
    Busca por ISSUE KEY (ej. 'UCM-62') en CSV_DATA (memoria) y devuelve la fila encontrada como JSON.
    """
    target_key = query.strip().upper()
    if target_key in CSV_DATA:
        return json.dumps(CSV_DATA[target_key], ensure_ascii=False, indent=2)
    return "Query returned no results."

@tool
def update_jira_issue_tool(wrapper_str: str) -> str:
    """
    Wrapper para LangChain. Espera JSON string: {"key":"UCM-62","payload":{...}}
    """
    try:
        data = json.loads(wrapper_str)
        key = data.get("key")
        payload = data.get("payload") or data.get("fields") or {}
        return update_jira_issue_api(key, json.dumps({"fields": payload}, ensure_ascii=False))
    except Exception as e:
        return f"tool error: {e}"

def run_correction_flow(task_key: str, user_instruction: str):
    print(f"\n--- 0. Actualizando CSV desde Jira... ---")
    # 1. Actualiza el CSV antes de cada tarea
    fetch_and_save_issues()
    
    global CSV_DATA
    CSV_DATA = load_csv_to_memory()
    
    print(f"\n--- 1. Obteniendo datos para {task_key} desde memoria ---")
    
    if task_key not in CSV_DATA:
        return f"FALLO: No se encontró el issue {task_key} en los datos cargados."
    
    issue_data = json.dumps(CSV_DATA[task_key], ensure_ascii=False, indent=2)
    print(f"Datos recibidos:\n{issue_data}")
    
    print("\n--- 2. El LLM está generando un plan basado en tu instrucción... ---")
    
    # Usa el LLM y prompt ya cargados
    chain = PAYLOAD_PROMPT | LLM
    llm_response_content = chain.invoke({
        "issue_data": issue_data, 
        "issue_key_aqui": task_key,
        "user_instruction": user_instruction 
    }).content
    
    print(f"Respuesta del LLM (completa):\n{llm_response_content}")
    
    if llm_response_content.strip().upper() == "NO_UPDATE":
        return f"ÉXITO: El LLM ha determinado que no se requieren cambios para {task_key}."
    
    try:
        print("\n--- 3. Extrayendo y Reestructurando Payload del LLM ---")
        
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', llm_response_content, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            json_match = re.search(r'(\{.*\})', llm_response_content, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                return "FALLO: No se encontró JSON válido en la respuesta del LLM."

        try:
            parsed_payload = json.loads(json_str)
        except json.JSONDecodeError:
            return f"FALLO: JSON encontrado no es válido: {json_str}"

        final_payload = {"fields": {}}
        
        if "fields" in parsed_payload:
            final_payload["fields"] = parsed_payload["fields"]
        elif task_key in parsed_payload:
            inner_data = parsed_payload[task_key]
            if isinstance(inner_data, dict):
                final_payload["fields"] = inner_data
            else:
                return f"FALLO: El valor de '{task_key}' no es un objeto válido."
        elif isinstance(parsed_payload, dict) and parsed_payload:
            final_payload["fields"] = parsed_payload
        else:
            return f"FALLO: No se pudo interpretar el payload del LLM: {parsed_payload}"

        final_payload_str = json.dumps(final_payload, ensure_ascii=False)
        print(f"Payload final a enviar a Jira: {final_payload_str}")

        result_message = update_jira_issue_api(task_key, final_payload_str)
        
        if "Success" in result_message:
            return f"ÉXITO: Issue {task_key} actualizado correctamente."
        else:
            return f"FALLO: La actualización falló. Respuesta de Jira: {result_message}"
            
    except Exception as e:
        return f"FALLO: Error inesperado al procesar la respuesta del LLM o actualizar Jira: {e}"

if __name__ == "__main__":
    print("Agente Autónomo de Jira iniciado. Escribe tu instrucción o 'salir' para terminar.")
    
    while True:
        user_input = input("\n> ¿Qué tarea quieres realizar?: ")
        
        if user_input.lower() in ['salir', 'exit', 'quit']:
            break
            
        task_key = extract_issue_key(user_input)
        
        if not task_key:
            print("❌ Error: No se pudo encontrar una clave de issue válida (ej. 'UCM-62') en tu instrucción. Inténtalo de nuevo.")
            continue
            
        final_result = run_correction_flow(task_key, user_input)

        print("\n--- TAREA COMPLETADA ---")
        print(final_result)