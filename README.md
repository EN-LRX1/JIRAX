
# Jiracaf Agents — Agent V8

Un agente autónomo para detección de duplicados y actualizaciones en Jira que combina LLM local (Ollama), embeddings y una memoria vectorial (FAISS) para análisis contextual avanzado.

---

## Características clave

+- Uso de Ollama para generación de payloads (LLM local).
+- Memoria vectorial con FAISS y embeddings (nomic / Ollama Embeddings) para buscar issues similares.
+- Soporte para campos numéricos y texto enriquecido (ADF) en Jira.
+- Flujo por lotes o por issue; enfoque en detección de duplicados y actualización automática de `customfield_10602`.

---

## Requisitos mínimos

+- Python 3.9+
+- Acceso a JSM y API token con permiso de edición
+- Ollama (local) para LLM y embeddings
+- Paquetes Python (ver `pyproject.toml`)

---

## Variables de entorno importantes

Colocar en un archivo `.env` en la raíz del repo:

```
JIRA_DOMAIN=tuinstancia.atlassian.net
EMAIL=tu_email@empresa.com
API_TOKEN=tu_token
OUTPUT_FILE=ucm_issues.csv
JIRA_VERIFY=false          
LLM_MODEL=gpt-oss:latest     
EMBEDDING_MODEL=nomic-embed-text
```

---

## Cómo ejecutar `agent_v8.py`

1. Asegúrate de tener Ollama corriendo y el modelo/embedding disponibles.
2. Carga issues desde Jira a CSV (el script `fetcher_sql.py` lo hace automáticamente al iniciar).
3. Ejecuta:

```bash
python agent_v8.py
```

Flujo interactivo: el agente pedirá una instrucción; puedes indicar una clave específica o dejar que procese múltiples issues, por ejemplo:

```
> What task would you like to perform?: Analyze cluster for duplicates
```

O puedes pedir acciones sobre issues concretos, p. ej. `UCM-62` o `UCM-64`.

---

## Qué hace internamente V8

+- `fetcher_sql.fetch_and_save_issues()` obtiene issues via API y guarda `ucm_issues.csv`.
+- `load_csv_to_memory()` carga el CSV en memoria (diccionario por key). 
+- `build_vector_store()` crea un FAISS a partir de summaries + contenido (usa `OllamaEmbeddings`).
+- Para cada issue objetivo, se genera contexto de issues similares (similarity_search) y se invoca al LLM con `PAYLOAD_GENERATION_TEMPLATE_V8`.
+- Se parsea el JSON devuelto por el LLM y se convierte en `{"fields": ...}` antes de llamar a la API.

---

## Notas sobre SSL y `JIRA_VERIFY`

+- Si tu Jira tiene un certificado válido, deja `JIRA_VERIFY=true` (recomendado).
+- Si usas un certificado autofirmado en un entorno de desarrollo, puedes poner `JIRA_VERIFY=false` para evitar errores SSL.

---

## Payloads y formatos soportados

+- Campos numéricos (ej. `customfield_10341`, `customfield_10342`) deben enviarse como números.
+- Campo ADF (`customfield_10193`) debe enviarse como documento Atlassian (ADF). El agente convierte texto plano a ADF cuando detecta que el LLM devuelve un string para este campo.
+- El agente filtra campos prohibidos antes de enviar a la API (p. ej. `key`, `status`, `assignee`, etc.).

---

## Ejemplo de ejecución (resumen)

1) Inicia el agente:

```bash
python agent_v8.py
```

2) Pedir detección de duplicados (proceso por issue):

```
> What task would you like to perform?: Find duplicates for UCM-62
```

Salida esperada (resumen):

```
LLM Response for UCM-62:
```json
{
  "customfield_10602": "❗ Issue may be repeated or similar to UCM-31, UCM-50"
}
```
SENDING: PUT https://.../issue/UCM-62 with Payload: {"fields": {"customfield_10602": "..."}}
```

---

## Estructura recomendada del proyecto

```
Jiracaf_Agents/
├── agent_v8.py         
├── fetcher_sql.py      
├── templates.py        
├── ucm_issues.csv    
├── .env
├── README.md
└── pyproject.toml
```

---

MIT
