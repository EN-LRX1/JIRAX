import requests
from requests.auth import HTTPBasicAuth
import json
import os
import csv
from dotenv import load_dotenv
import re
from langchain_community.vectorstores import FAISS
from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_core.prompts import PromptTemplate
from langchain_core.documents import Document
from templates import PAYLOAD_GENERATION_TEMPLATE_V6 as PAYLOAD_GENERATION_TEMPLATE
from fetcher_sql import fetch_and_save_issues
load_dotenv()
JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
EMAIL = os.getenv("EMAIL")
API_TOKEN = os.getenv("API_TOKEN")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-oss:latest")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
OUTPUT_FILE = os.getenv("OUTPUT_FILE", "ucm_issues.csv")
LLM = ChatOllama(model=LLM_MODEL, temperature=0)
EMBEDDINGS = OllamaEmbeddings(model=EMBEDDING_MODEL)
PAYLOAD_PROMPT = PromptTemplate.from_template(PAYLOAD_GENERATION_TEMPLATE)

def load_csv_to_memory():
    """Carga el CSV en memoria y lo devuelve como un diccionario."""
    if not os.path.exists(OUTPUT_FILE): return {}
    data = {}
    with open(OUTPUT_FILE, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = row.get("key")
            if key: data[key.strip().upper()] = row
    return data

def build_vector_store(all_issues_data):
    """Construye y devuelve una memoria vectorial a partir de los datos de los issues."""
    print(f"Creating vector store from {len(all_issues_data)} issues...")
    documents = [
        Document(
            page_content=f"Summary: {issue_data.get('summary', '')}\nDescription: {issue_data.get('customfield_10193', '')}",
            metadata={"key": key}
        ) for key, issue_data in all_issues_data.items()
    ]
    if not documents:
        return None
    vector_store = FAISS.from_documents(documents, EMBEDDINGS)
    print("Vector store created successfully.")
    return vector_store

def update_jira_issue_api(issue_key: str, update_payload_str: str) -> str:
    """Updates a Jira issue using the REST API."""
    allowed_test_issues = {"UCM-62", "UCM-64"}
    if issue_key not in allowed_test_issues:
        return f"Permission Error: Test agent can only modify specified test issues."

    if not all([JIRA_DOMAIN, EMAIL, API_TOKEN]):
        return "Error: Missing Jira credentials."

    url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/{issue_key}"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    try:
        payload = json.loads(update_payload_str)
        fields_to_update = (payload.get("fields") or {}).copy()
        
        prohibited_fields = ["key", "status", "assignee"]
        for k in prohibited_fields: fields_to_update.pop(k, None)
        
        if not fields_to_update:
            return f"Skipped: No valid fields to update for {issue_key}."

        jira_payload = {"fields": fields_to_update}
        print(f"SENDING: PUT {url} with Payload: {json.dumps(jira_payload, ensure_ascii=False)}")
        response = requests.put(
            url, headers=headers, data=json.dumps(jira_payload),
            auth=HTTPBasicAuth(EMAIL, API_TOKEN), verify=False, timeout=30
        )
        if response.status_code == 204:
            return f"Success: Issue {issue_key} updated."
        return f"Error: Status {response.status_code}, {response.text}"
    except Exception as e:
        return f"An exception occurred: {e}"

def extract_all_issue_keys(text: str, all_known_keys: list) -> list[str]:
    """Extrae claves de issue del texto. Si no hay, devuelve todas las claves conocidas."""
    matches = re.findall(r'([A-Z]{2,}-\d+)', text.upper())
    if not matches:
        print("No specific issue keys found, targeting ALL known issues.")
        return sorted(all_known_keys)
    return sorted(list(set(matches)))

def process_single_issue(issue_key: str, user_instruction: str, all_issues_data: dict, vector_store, tasks_pending_set: set):
    """
    Procesa un único issue, consultando al LLM.
    Devuelve el mensaje de resultado y la clave del duplicado encontrado (si existe).
    """
    print(f"\n--- Analyzing Issue: {issue_key} ---")
    current_issue = all_issues_data.get(issue_key)
    if not current_issue:
        return f"{issue_key}: Skipped (not found in local data).", None

    try:
        query_content = f"Summary: {current_issue.get('summary', '')}\nDescription: {current_issue.get('customfield_10193', '')}"
        similar_docs = vector_store.similarity_search(query_content, k=4)
        
        similar_issues_context = ""
        for doc in similar_docs:
            if doc.metadata["key"] != issue_key:
                key = doc.metadata["key"]
                data = all_issues_data.get(key, {})
                similar_issues_context += (
                    f"- ISSUE {key}:\n"
                    f"  Summary: {data.get('summary', 'N/A')}\n"
                    f"  Description: {data.get('customfield_10193', 'N/A')}\n\n"
                )
        if not similar_issues_context:
            similar_issues_context = "No similar issues found in the vector memory."
        
        chain = PAYLOAD_PROMPT | LLM
        llm_response_content = chain.invoke({
            "current_issue_data": json.dumps(current_issue, indent=2),
            "current_issue_key": issue_key,
            "user_instruction": user_instruction,
            "similar_issues_context": similar_issues_context
        }).content
        
        print(f"LLM Response for {issue_key}:\n{llm_response_content}")

        match = re.search(r'```json\s*(\{.*?\})\s*```', llm_response_content, re.DOTALL)
        if not match:
            return f"{issue_key}: Skipped (no payload generated).", None
        
        # --- Lógica de Metacognición (Extracción) ---
        found_dup_key = None
        payload_json = json.loads(match.group(1))
        duplicate_text = payload_json.get("customfield_10602", "")
        
        dup_match = re.search(r"similar to ([A-Z]{2,}-\d+)", duplicate_text)
        if dup_match:
            key = dup_match.group(1)
            if key in tasks_pending_set:
                found_dup_key = key
        
        final_payload_str = json.dumps({"fields": payload_json})
        api_result_message = update_jira_issue_api(issue_key, final_payload_str)
        
        return f"{issue_key}: {api_result_message}", found_dup_key

    except Exception as e:
        return f"Skipped {issue_key}: Unexpected error ({e}).", None

def main():
    """Función principal que inicializa y ejecuta el bucle del agente."""
    print("--- Jira Autonomous Agent Initializing ---")
    
    fetch_and_save_issues()
    all_issues_data = load_csv_to_memory()
    if not all_issues_data:
        print("❌ Error: No issues loaded from CSV. Cannot proceed.")
        return

    vector_store = build_vector_store(all_issues_data)
    if not vector_store:
        print("❌ Error: Could not build vector store.")
        return

    print("\nInitialization complete. Agent is ready.")
    
    while True:
        user_input = input("\n> What task would you like to perform?: ")
        if user_input.lower() in ['exit', 'quit', 'salir']:
            break
        
        keys_to_process = extract_all_issue_keys(user_input, list(all_issues_data.keys()))
        tasks_pending = set(keys_to_process) 
        final_results = []
            
        print(f"\n--- 1. Targetting {len(tasks_pending)} issue(s) for processing ---")
        
        # --- BUCLE DE TRABAJO CON METACOGNICIÓN ---
        while tasks_pending:
            current_key = tasks_pending.pop()
            
            result_msg, found_dup_key = process_single_issue(
                current_key, 
                user_input, 
                all_issues_data, 
                vector_store, 
                tasks_pending # Pasamos el set para que pueda comprobarlo
            )
            
            final_results.append(result_msg)
            
            # --- METACOGNICIÓN EN ACCIÓN ---
            if found_dup_key and found_dup_key in tasks_pending:
                # Si encontramos un duplicado que AÚN está pendiente...
                tasks_pending.remove(found_dup_key) # ¡Lo quitamos de la lista!
                
                print(f"\n--- Metacognition: Auto-updating {found_dup_key} as duplicate of {current_key} ---")
                
                # Construimos y enviamos su payload sin llamar al LLM
                dup_payload_str = json.dumps({
                    "fields": {
                        "customfield_10602": f"❗ Issue may be repeated or similar to {current_key}"
                    }
                })
                dup_result_msg = update_jira_issue_api(found_dup_key, dup_payload_str)
                final_results.append(f"{found_dup_key}: {dup_result_msg}")
        
        summary = f"✅ PROCESS COMPLETE\n\nDetails:\n- " + "\n- ".join(final_results)
        print(summary)

if __name__ == "__main__":
    main()