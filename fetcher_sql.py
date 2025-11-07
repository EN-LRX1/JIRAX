import requests
from requests.auth import HTTPBasicAuth
import json
import csv
import os
import time
import tempfile
import sqlite3
import base64
from dotenv import load_dotenv

# --- CONFIGURAZIOA ---
load_dotenv()
PROJECT_KEY = os.getenv("PROJECT_KEY", "UCM")
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "100"))
OUTPUT_CSV = os.getenv("OUTPUT_CSV", "ucm_issues.csv")
OUTPUT_DB = os.getenv("OUTPUT_DB", "ucm_issues.db")
JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
EMAIL = os.getenv("EMAIL")
API_TOKEN = os.getenv("API_TOKEN")
JIRA_VERIFY = os.getenv("JIRA_VERIFY", "true").lower() not in ("0", "false", "no")

# --- HELPERS ---
def get_safe_value(fields, key):
    v = fields.get(key)
    if isinstance(v, dict):
        return v.get("value") or v.get("displayName") or ""
    if v is None:
        return ""
    return str(v)

def get_doc_text(fields, key):
    doc_data = fields.get(key)
    try:
        content = doc_data.get("content", []) if isinstance(doc_data, dict) else []
        text_parts = []
        for block in content:
            for inline in block.get("content", []):
                if inline.get("type") == "text":
                    text_parts.append(inline.get("text", ""))
        return " ".join(text_parts)
    except Exception:
        return ""

def atomic_write_csv(path, headers, rows):
    fd, tmp = tempfile.mkstemp(prefix="tmp_ucm_", suffix=".csv")
    os.close(fd)
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(headers)
            w.writerows(rows)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass

# --- SQLITE SETUP ---
def init_sqlite(db_path):
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("""
    CREATE TABLE IF NOT EXISTS issues (
        key TEXT PRIMARY KEY,
        summary TEXT,
        business TEXT,
        area TEXT,
        owner TEXT,
        assignee TEXT,
        status TEXT,
        main_impact_type TEXT,
        type_field TEXT,
        value_field TEXT,
        feasibility TEXT,
        prioridad TEXT,
        riesgo TEXT,
        transversal TEXT,
        created TEXT,
        updated TEXT,
        descripcion TEXT,
        decision TEXT,
        raw_json TEXT,
        last_seen INTEGER
    );
    """)
    con.commit()
    return con

def upsert_issue_sqlite(con, rec):
    sql = """
    INSERT INTO issues(key, summary, business, area, owner, assignee, status,
        main_impact_type, type_field, value_field, feasibility, prioridad,
        transversal, riesgo, created, updated, descripcion, decision, raw_json, last_seen)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(key) DO UPDATE SET
      summary=excluded.summary,
      business=excluded.business,
      area=excluded.area,
      owner=excluded.owner,
      assignee=excluded.assignee,
      status=excluded.status,
      main_impact_type=excluded.main_impact_type,
      type_field=excluded.type_field,
      value_field=excluded.value_field,
      feasibility=excluded.feasibility,
      prioridad=excluded.prioridad,
      riesgo=excluded.riesgo,
      transversal=excluded.transversal,
      created=excluded.created,
      updated=excluded.updated,
      descripcion=excluded.descripcion,
      decision=excluded.decision,
      raw_json=excluded.raw_json,
      last_seen=excluded.last_seen;
    """
    con.execute(sql, rec)
    con.commit()

def fetch_all_issues():
    JIRA_DOMAIN = os.environ.get("JIRA_DOMAIN")
    EMAIL       = os.environ.get("EMAIL")
    API_TOKEN   = os.environ.get("API_TOKEN")
    PROJECT_KEY = os.environ.get("PROJECT_KEY", "UCM")
    MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "100"))

    if not all([JIRA_DOMAIN, EMAIL, API_TOKEN]):
        raise RuntimeError("Faltan JIRA_DOMAIN, EMAIL o API_TOKEN en el entorno.")

    url = f"https://{JIRA_DOMAIN}/rest/api/3/search/jql"

    fields_list = [
        "summary",
        "assignee",
        "status",
        "created",
        "updated",
        "customfield_10190",  # Business
        "customfield_10191",  # Area
        "customfield_10192",  # Owner
        "customfield_10196",  # Main Impact Type
        "customfield_10194",  # Type
        "customfield_10220",  # Value
        "customfield_10221",  # Feasibility
        "customfield_10222",  # Prioridad
        "customfield_10248",  # Riesgo
        "customfield_10213",  # Transversal
        "customfield_10193",  # Descripción / Objectives
        "customfield_10536",  # Decision
    ]
    fields_param = ",".join(fields_list)

    session = requests.Session()
    session.auth = HTTPBasicAuth(EMAIL, API_TOKEN)  
    #session.verify = False # Solo dentro de la empresa por proxy y firewall!!!
    headers = {"Accept": "application/json"}
    base_params = {
        "jql": f"project={PROJECT_KEY}",
        "maxResults": MAX_RESULTS,
        "fields": fields_param
    }

    all_issues = []
    next_token = None
    page = 1

    while True:
        params = dict(base_params)
        if next_token:
            params["nextPageToken"] = next_token

        try:
            resp = session.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"Error al llamar a JIRA: {e}")
            break

        data = resp.json()
        issues = data.get("issues", [])
        all_issues.extend(issues)
        print(f"Página {page}: recibidas {len(issues)} issues (acumuladas: {len(all_issues)}).")
        next_token = data.get("nextPageToken")
        if not next_token:
            break

        page += 1
        time.sleep(0.2)  

    return all_issues


# --- MAIN: fetch, write CSV and upsert SQLite ---
def fetch_and_save_issues():
    issues = fetch_all_issues()
    if not issues:
        print("No se obtuvieron issues.")
        return

    headers_list = [
        "key", "summary", "customfield_10190", "customfield_10191", "customfield_10192", "assignee",
        "status", "customfield_10196", "customfield_10194", "customfield_10341", "customfield_10342",
        "customfield_10222", "customfield_10248", "customfield_10213", "created", "updated", "customfield_10193", "customfield_10536"
    ]

    csv_rows = []
    con = init_sqlite(OUTPUT_DB)

    for issue in issues:
        fields = issue.get("fields", {}) or {}
        issue_key = issue.get("key", "")

        summary   = fields.get("summary", "") or ""
        owner_value = get_safe_value(fields, "customfield_10192")
        business_value = get_safe_value(fields, "customfield_10190")
        area_value     = get_safe_value(fields, "customfield_10191")
        main_impact_type_value = get_safe_value(fields, "customfield_10196")
        type_value  = get_safe_value(fields, "customfield_10194")
        value_value = get_safe_value(fields, "customfield_10220")
        feasibility_value = get_safe_value(fields, "customfield_10221")
        prioridad_value   = get_safe_value(fields, "customfield_10222")
        riesgo_value      = get_safe_value(fields, "customfield_10248")
        transversal       = get_safe_value(fields, "customfield_10213")
        assignee_value = (fields.get("assignee") or {}).get("displayName", "")
        status_value   = (fields.get("status") or {}).get("name", "")
        created = fields.get("created", "")
        updated = fields.get("updated", "")
        descripcion_objectives = get_doc_text(fields, "customfield_10193")
        decision_value = get_safe_value(fields, "customfield_10536")

        # CSV row
        csv_rows.append([
            issue_key,
            summary,
            business_value,
            area_value,
            owner_value,
            assignee_value,
            status_value,
            main_impact_type_value,
            type_value,
            value_value,
            feasibility_value,
            prioridad_value,
            riesgo_value,
            transversal,
            created,
            updated,
            descripcion_objectives,
            decision_value
        ])

        # upsert SQLite (raw_json para debug)
        raw_json = json.dumps(issue, ensure_ascii=False)
        last_seen = int(time.time())
        rec = (
            issue_key,
            summary,
            business_value,
            area_value,
            owner_value,
            assignee_value,
            status_value,
            main_impact_type_value,
            type_value,
            value_value,
            feasibility_value,
            prioridad_value,
            riesgo_value,
            transversal,
            created,
            updated,
            descripcion_objectives,
            decision_value,
            raw_json,
            last_seen
        )
        upsert_issue_sqlite(con, rec)

    atomic_write_csv(OUTPUT_CSV, headers_list, csv_rows)
    con.close()
    print(f"CSV guardado en: {OUTPUT_CSV}")
    print(f"SQLite guardado en: {OUTPUT_DB} (tabla 'issues')")


# --- RUN ---
if __name__ == "__main__":
    fetch_and_save_issues()
