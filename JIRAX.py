import requests
from requests.auth import HTTPBasicAuth
import json
import os
import csv
from dotenv import load_dotenv
from langchain_ollama import ChatOllama
from langchain_core.prompts import PromptTemplate
from langchain_core.tools import tool
from templates import PAYLOAD_GENERATION_TEMPLATE_V2
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
PAYLOAD_PROMPT = PromptTemplate.from_template(PAYLOAD_GENERATION_TEMPLATE_V2)

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
    if issue_key not in ("UCM-62", "UCM-64"):
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
        # GUARDIAN PARA EVITAR QUE CAMBIOS SIN PERMISO
        if "key" in fields_to_update:
            del fields_to_update["key"]
        if "status" in fields_to_update:
            del fields_to_update["status"]
        if "assignee" in fields_to_update:
            del fields_to_update["assignee"]
        if "customfield_10190" in fields_to_update:
            del fields_to_update["customfield_10190"]
        if "customfield_10191" in fields_to_update:
            del fields_to_update["customfield_10191"]
        if "customfield_10196" in fields_to_update:
            del fields_to_update["customfield_10196"]
        if "customfield_10194" in fields_to_update:
            del fields_to_update["customfield_10194"]
        if "customfield_10222" in fields_to_update:
            del fields_to_update["customfield_10222"]
        if "customfield_10213" in fields_to_update:
            del fields_to_update["customfield_10213"]

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

def extract_all_issue_keys(text: str) -> list[str]:
    """Extrae todas las claves de issue del texto."""
    matches = re.findall(r'([A-Z]{2,}-\d+)', text.upper())
    return list(set(matches))  

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

def run_correction_flow(user_instruction: str):
    print(f"\n--- 0. Actualizando CSV desde Jira... ---")
    # 1. Actualiza el CSV antes de cada tarea
    fetch_and_save_issues()
    
    global CSV_DATA
    CSV_DATA = load_csv_to_memory()
    
    # 2. Extrae todas las claves de issue del input
    all_issue_keys = extract_all_issue_keys(user_instruction)
    
    if not all_issue_keys:
        return "❌ Error: No se encontraron claves de issue válidas en tu instrucción."
    
    print(f"\n--- 1. Procesando {len(all_issue_keys)} issue(s): {', '.join(all_issue_keys)} ---")
    
    # 3. Obtén datos de todos los issues
    all_issues_data = {}
    for key in all_issue_keys:
        if key not in CSV_DATA:
            print(f"⚠️  Advertencia: No se encontró el issue {key} en los datos cargados.")
            continue
        all_issues_data[key] = json.dumps(CSV_DATA[key], ensure_ascii=False, indent=2)
    
    if not all_issues_data:
        return "FALLO: No se encontraron datos para ninguno de los issues."
    
    print("\n--- 2. El LLM está generando un plan basado en tu instrucción... ---")
    
    # 4. Prepara el prompt con todos los issues
    all_issues_str = "\n\n".join([
        f"**DATOS DEL ISSUE ({key}):** {data}" 
        for key, data in all_issues_data.items()
    ])
    
    # Usa el LLM y prompt ya cargados
    chain = PAYLOAD_PROMPT | LLM
    llm_response_content = chain.invoke({
        "all_issues_data": all_issues_str,
        "issue_keys": ", ".join(all_issue_keys),
        "user_instruction": user_instruction 
    }).content
    
    print(f"Respuesta del LLM (completa):\n{llm_response_content}")
    
    if llm_response_content.strip().upper() == "NO_UPDATE":
        return f"ÉXITO: El LLM ha determinado que no se requieren cambios para los issues."
    
    try:
        print("\n--- 3. Extrayendo y Reestructurando Payloads del LLM ---")
        
        # Busca todos los bloques JSON en la respuesta
        json_matches = re.findall(r'```json\s*(\{.*?\})\s*```', llm_response_content, re.DOTALL)
        
        if not json_matches:
            return "FALLO: No se encontraron JSON válidos en la respuesta del LLM."
        
        # Procesa cada payload encontrado
        processed_count = 0
        remaining_issue_keys = set(all_issue_keys)  # Claves pendientes
        
        for json_str in json_matches:
            try:
                parsed_payload = json.loads(json_str)
                
                # --- NUEVA LÓGICA MEJORADA: Detectar el formato del payload ---
                issue_key = None
                fields_to_update = {}
                
                # Caso 1: El payload tiene la estructura esperada {"UCM-62": {...}}
                for key in remaining_issue_keys:
                    if key in parsed_payload:
                        issue_key = key
                        fields_to_update = parsed_payload[key]
                        break
                
                # Caso 2: El payload es directamente un objeto de campos (sin clave superior)
                # Intentamos inferir la clave del issue de los comentarios o contexto
                if not issue_key:
                    for key in remaining_issue_keys:
                        # Busca la clave de issue en el texto antes del bloque JSON
                        pattern = rf'Payload.*?\b{key}\b'
                        if re.search(pattern, llm_response_content.split(json_str)[0], re.IGNORECASE):
                            issue_key = key
                            fields_to_update = parsed_payload
                            break
                
                # Caso 3: Asignar en orden si no se puede inferir
                if not issue_key and remaining_issue_keys:
                    # Asigna el siguiente payload al siguiente issue en orden
                    issue_key = all_issue_keys[len(all_issue_keys) - len(remaining_issue_keys)]
                    fields_to_update = parsed_payload
                
                if not issue_key:
                    print(f"⚠️  No se pudo inferir la clave de issue para: {parsed_payload}")
                    continue
                
                # Verifica que el issue_key sea uno de los válidos
                if issue_key not in all_issue_keys:
                    print(f"⚠️  Clave de issue '{issue_key}' no está en la lista original: {all_issue_keys}")
                    continue
                
                # Marcar como procesado
                remaining_issue_keys.discard(issue_key)
                
                # Extrae los campos a actualizar
                final_payload = {"fields": {}}
                
                if isinstance(fields_to_update, dict):
                    final_payload["fields"] = fields_to_update
                else:
                    print(f"FALLO: El valor de campos para '{issue_key}' no es un objeto válido: {fields_to_update}")
                    continue

                final_payload_str = json.dumps(final_payload, ensure_ascii=False)
                print(f"Payload final a enviar a Jira ({issue_key}): {final_payload_str}")

                result_message = update_jira_issue_api(issue_key, final_payload_str)
                
                if "Success" in result_message:
                    print(f"✅ ÉXITO: Issue {issue_key} actualizado correctamente.")
                    processed_count += 1
                else:
                    print(f"❌ FALLO: La actualización de {issue_key} falló. {result_message}")
                    
            except json.JSONDecodeError:
                print(f"FALLO: JSON encontrado no es válido: {json_str}")
                continue
            except Exception as e:
                print(f"FALLO: Error procesando payload: {e}")
                continue
        
        return f"✅ PROCESO COMPLETADO: Se procesaron {processed_count} updates de {len(json_matches)} encontrados."
            
    except Exception as e:
        return f"FALLO: Error inesperado al procesar la respuesta del LLM o actualizar Jira: {e}"

if __name__ == "__main__":
    print("Agente Autónomo de Jira iniciado. Escribe tu instrucción o 'salir' para terminar.")
    
    while True:
        user_input = input("\n> ¿Qué tarea quieres realizar?: ")
        
        if user_input.lower() in ['salir', 'exit', 'quit']:
            break
            
        final_result = run_correction_flow(user_input)

        print("\n--- TAREA COMPLETADA ---")
        print(final_result)