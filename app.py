"""
BW Migration Intelligence Platform — Streamlit Edition
Converts the HTML/JS tool to a Python/Streamlit web app with:
  - Google login (Streamlit Community Cloud built-in)
  - Supabase persistent storage for uploads and PM plans
  - Full BW ZIP analysis engine in Python
  - Multi-user: shared team data, per-user audit trail
"""

import streamlit as st
import zipfile, io, re, json, os
from datetime import datetime
from pathlib import Path

# ── Page config (must be first Streamlit call) ────────────────────────────
st.set_page_config(
    page_title="BW Migration Intelligence",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Imports that need pip install ─────────────────────────────────────────
try:
    import pandas as pd
    import plotly.graph_objects as go
    import plotly.express as px
    from supabase import create_client, Client
except ImportError as e:
    st.error(f"Missing dependency: {e}. Run: pip install -r requirements.txt")
    st.stop()

# ── Database helpers ───────────────────────────────────────────────────────
@st.cache_resource
def get_supabase() -> Client:
    """Connect to Supabase — credentials stored in Streamlit secrets."""
    url  = st.secrets["supabase"]["url"]
    key  = st.secrets["supabase"]["anon_key"]
    return create_client(url, key)

def db_log_upload(user_email: str, filename: str, uc_count: int, provider_count: int):
    """Record every ZIP upload with user + timestamp."""
    try:
        get_supabase().table("uploads").insert({
            "user_email":     user_email,
            "filename":       filename,
            "uc_count":       uc_count,
            "provider_count": provider_count,
            "uploaded_at":    datetime.utcnow().isoformat(),
        }).execute()
    except Exception as e:
        st.warning(f"Could not log upload: {e}")

def db_save_pm_plan(user_email: str, plan: dict):
    """Upsert the PM plan for a session (team, assignments, ucMeta)."""
    try:
        get_supabase().table("pm_plans").upsert({
            "user_email": user_email,
            "plan_json":  json.dumps(plan),
            "updated_at": datetime.utcnow().isoformat(),
        }, on_conflict="user_email").execute()
        return True
    except Exception as e:
        st.warning(f"Could not save plan: {e}")
        return False

def db_load_pm_plan(user_email: str) -> dict | None:
    """Load the most recent PM plan for this user."""
    try:
        res = get_supabase().table("pm_plans")\
            .select("plan_json")\
            .eq("user_email", user_email)\
            .single()\
            .execute()
        if res.data:
            return json.loads(res.data["plan_json"])
    except Exception:
        pass
    return None

def db_get_upload_history(user_email: str) -> list:
    """Return the last 20 uploads for this user."""
    try:
        res = get_supabase().table("uploads")\
            .select("*")\
            .eq("user_email", user_email)\
            .order("uploaded_at", desc=True)\
            .limit(20)\
            .execute()
        return res.data or []
    except Exception:
        return []

def db_get_all_usage() -> list:
    """Admin: return all upload events across all users."""
    try:
        res = get_supabase().table("uploads")\
            .select("user_email, filename, uc_count, uploaded_at")\
            .order("uploaded_at", desc=True)\
            .limit(200)\
            .execute()
        return res.data or []
    except Exception:
        return []

# ── Authentication ─────────────────────────────────────────────────────────
def get_current_user() -> str | None:
    """
    On Streamlit Community Cloud with Google login enabled,
    st.experimental_user is populated automatically.
    Returns the user's email or None if not logged in.
    """
    user = st.context.headers.get("X-Streamlit-User", None)
    if user:
        return user
    # Streamlit Community Cloud native auth
    if hasattr(st, "user") and st.user.is_logged_in:
        return st.user.email
    return None

def require_login():
    """Show login prompt if not authenticated. Returns user email."""
    user = get_current_user()
    if user:
        return user
    st.markdown("""
    <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;
                height:60vh;gap:20px;">
        <h1 style="color:#00d4ff;">⚡ BW Migration Intelligence</h1>
        <p style="color:#94a3b8;font-size:16px;">SAP BW to Cloud Migration Analysis Platform</p>
    </div>
    """, unsafe_allow_html=True)
    # Streamlit Community Cloud handles Google login automatically
    # when login is enabled in app settings
    st.info("🔐 Please log in with your Google account to continue.")
    st.stop()

# ══════════════════════════════════════════════════════════════════════════
# ANALYSIS ENGINE — Python port of the JS parsing and scoring logic
# ══════════════════════════════════════════════════════════════════════════

PLATFORMS = {
    "databricks": {"name": "Databricks", "icon": "⚡", "modifier": 1.0},
    "snowflake":  {"name": "Snowflake",  "icon": "❄",  "modifier": 1.1},
    "adf":        {"name": "ADF",        "icon": "🔷", "modifier": 1.35},
    "synapse":    {"name": "Synapse",    "icon": "⚡",  "modifier": 1.2},
    "fabric":     {"name": "MS Fabric",  "icon": "🌐", "modifier": 1.15},
    "glue":       {"name": "AWS Glue",   "icon": "☁",  "modifier": 1.25},
}

EFFORT = {
    "Very High": (80, 120),
    "High":      (40, 80),
    "Medium":    (20, 40),
    "Low":       (5,  20),
}

def complexity_label(score: float) -> str:
    if score > 120: return "Very High"
    if score > 40:  return "High"
    if score > 15:  return "Medium"
    return "Low"

def complexity_color(label: str) -> str:
    return {"Very High": "#ff4466", "High": "#ffaa00",
            "Medium": "#00d4ff", "Low": "#00e676"}.get(label, "#888")

def estimate_hours(complexity_label: str, modifier: float = 1.0) -> int:
    low, high = EFFORT.get(complexity_label, (5, 20))
    return round(((low + high) / 2) * modifier)


class BwZipParser:
    """
    Parses BW extraction ZIP archives.
    Mirrors the JS parseUseCaseZip / parseProvider logic.
    """

    def __init__(self, zip_bytes: bytes):
        self.zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        self.paths = [p for p in self.zf.namelist() if not p.endswith("/")]

    def read(self, path: str) -> str:
        try:
            return self.zf.read(path).decode("utf-8", errors="replace")
        except Exception:
            return ""

    def find(self, *substrings) -> list[str]:
        result = []
        for p in self.paths:
            if all(s.lower() in p.lower() for s in substrings):
                result.append(p)
        return result

    # ── Detect ZIP type ────────────────────────────────────────────────────
    def is_fm_dump(self) -> bool:
        return any(re.search(r"/(LINEAGE|INTERFACE|DEPENDENCIES)\.txt$", p, re.I)
                   for p in self.paths)

    def is_use_case(self) -> bool:
        return any("/Flow/" in p or "/Transformations/" in p or "/DDL/" in p
                   for p in self.paths)

    # ── Use Case parsing ───────────────────────────────────────────────────
    def parse_use_cases(self) -> list[dict]:
        base_paths = set()
        for p in self.paths:
            m = re.match(r"^(.*?)/(Flow|DDL|Transformations)/", p, re.I)
            if m:
                base_paths.add(m.group(1))

        all_parts = [bp.split("/") for bp in base_paths]
        roots     = {p[0] for p in all_parts}
        max_depth = max((len(p) for p in all_parts), default=1)
        has_wrapper = len(roots) == 1 and max_depth >= 3

        uc_map: dict[str, dict] = {}

        for base_path in sorted(base_paths):
            parts = base_path.split("/")
            if has_wrapper:
                uc_name = parts[1] if len(parts) >= 2 else parts[0]
                prov_name = "/".join(parts[2:]) if len(parts) >= 3 else parts[-1]
            elif len(parts) >= 2:
                uc_name  = parts[0]
                prov_name = "/".join(parts[1:])
            else:
                uc_name = prov_name = parts[0]

            if not prov_name:
                prov_name = uc_name

            if uc_name not in uc_map:
                uc_map[uc_name] = {"name": uc_name, "providers": []}

            provider = self._parse_provider(base_path, prov_name)
            if provider:
                uc_map[uc_name]["providers"].append(provider)

        return list(uc_map.values())

    def _parse_provider(self, base: str, name: str) -> dict | None:
        # Transformations
        trans_paths = self.find(base, "Transformations")
        transformations = []
        for tp in trans_paths:
            if not tp.endswith(".txt"):
                continue
            content = self.read(tp)
            trans = self._parse_transformation(content, tp)
            transformations.append(trans)

        # Dependencies
        deps = []
        dep_paths = self.find(base, "Dependencies")
        for dp in dep_paths:
            if dp.endswith(".txt") or dp.endswith(".csv"):
                deps.extend(self._parse_dependencies(self.read(dp)))

        # Flow edges
        flow_edges = []
        for fp in self.find(base, "Flow"):
            if fp.endswith(".csv") or fp.endswith(".txt"):
                flow_edges.extend(self._parse_flow(self.read(fp)))

        # DDL tables
        tables = {"bronze": [], "silver": [], "gold": []}
        for dp in self.find(base, "DDL"):
            if dp.endswith(".sql") or dp.endswith(".txt"):
                layer = ("gold" if "gold" in dp.lower()
                         else "silver" if "silver" in dp.lower()
                         else "bronze")
                tbl = Path(dp).stem
                tables[layer].append(tbl)

        # DSOs and InfoCubes
        dsos  = [t["name"] for t in transformations if "_DSO" in t.get("name","").upper() or "DSO" in t.get("target","").upper()]
        cubes = [t["name"] for t in transformations if "CUBE" in t.get("name","").upper() or "ICUBE" in t.get("target","").upper()]

        # Compute complexity score
        score = self._compute_complexity(transformations, deps)
        label = complexity_label(score)

        return {
            "name":            name,
            "transformations": transformations,
            "dependencies":    deps,
            "flowEdges":       flow_edges,
            "tables":          tables,
            "dsos":            list(set(dsos)),
            "cubes":           list(set(cubes)),
            "complexity":      score,
            "complexityLabel": label,
            "sourcePath":      base,
        }

    def _parse_transformation(self, content: str, path: str) -> dict:
        lines = content.splitlines()
        name = Path(path).stem
        has_routine = bool(re.search(r"FORM\s+compute_data_package|START-OF-SELECTION", content, re.I))
        is_inactive = bool(re.search(r"inactive|status.*=.*inactive", content, re.I))
        routine_lines = len([l for l in lines if l.strip() and not l.strip().startswith("*")])
        return {
            "name":         name,
            "hasRoutine":   has_routine,
            "isInactive":   is_inactive,
            "routineLines": routine_lines,
            "source":       content[:200],
        }

    def _parse_dependencies(self, content: str) -> list[dict]:
        deps = []
        for line in content.splitlines():
            parts = [p.strip() for p in line.split(";")]
            if len(parts) >= 2:
                kind = parts[0].upper() if parts[0].upper() in ("FM","TABLE","METHOD","CLASS") else "FM"
                deps.append({"kind": kind, "name": parts[-1] if len(parts) > 1 else parts[0]})
        return deps

    def _parse_flow(self, content: str) -> list[dict]:
        edges = []
        for line in content.splitlines():
            parts = [p.strip() for p in line.split(";")]
            if len(parts) >= 2:
                edges.append({"from": parts[0], "to": parts[1]})
        return edges

    def _compute_complexity(self, transformations: list, deps: list) -> float:
        """Mirror of JS computeProviderComplexity()"""
        score = 0
        fms      = [d for d in deps if d["kind"] == "FM"]
        tables   = [d for d in deps if d["kind"] == "TABLE"]
        methods  = [d for d in deps if d["kind"] == "METHOD"]
        unique_fms = len({d["name"] for d in fms})
        score += unique_fms * 3
        score += len(methods) * 0.5
        active_trans = [t for t in transformations if not t.get("isInactive")]
        score += len([t for t in active_trans if t.get("hasRoutine")]) * 2
        score += len(active_trans) * 1
        score += len(tables) * 1
        return round(score)

    # ── FM Library parsing ─────────────────────────────────────────────────
    def parse_fm_library(self) -> dict[str, dict]:
        fm_lib = {}
        fm_folders = set()
        for p in self.paths:
            m = re.match(r"^(.*?)/(INTERFACE|DEPENDENCIES|LINEAGE)\.txt$", p, re.I)
            if m:
                fm_folders.add(m.group(1))

        for folder in fm_folders:
            fm_name = folder.split("/")[-1]
            interface   = self.read(f"{folder}/INTERFACE.txt")
            deps_txt    = self.read(f"{folder}/DEPENDENCIES.txt")
            lineage_txt = self.read(f"{folder}/LINEAGE.txt")
            source_txt  = self.read(f"{folder}/SOURCE.txt") or self.read(f"{folder}/ABAP.txt")

            children = re.findall(r"\b([YZ][_A-Z0-9]+)\b", deps_txt)
            is_custom = bool(re.match(r"^[YZ]", fm_name, re.I))
            lines = len(source_txt.splitlines()) if source_txt else 0

            fm_lib[fm_name] = {
                "name":      fm_name,
                "isCustom":  is_custom,
                "lines":     lines,
                "children":  list(set(children)),
                "interface": interface[:500],
                "source":    source_txt[:2000] if source_txt else "",
            }

        return fm_lib


def analyse_zip(zip_bytes: bytes) -> dict:
    """Full analysis pipeline. Returns dict with use_cases + fm_library."""
    parser = BwZipParser(zip_bytes)
    result = {"use_cases": [], "fm_library": {}, "errors": []}

    try:
        if parser.is_fm_dump() or not parser.is_use_case():
            result["fm_library"] = parser.parse_fm_library()
        if parser.is_use_case() or not parser.is_fm_dump():
            result["use_cases"] = parser.parse_use_cases()
        if not parser.is_fm_dump() and not parser.is_use_case():
            # Try both
            result["fm_library"] = parser.parse_fm_library()
            result["use_cases"]  = parser.parse_use_cases()
    except Exception as e:
        result["errors"].append(str(e))

    return result


# ══════════════════════════════════════════════════════════════════════════
# SESSION STATE HELPERS
# ══════════════════════════════════════════════════════════════════════════

def init_session():
    defaults = {
        "use_cases":  [],
        "fm_library": {},
        "platform":   "databricks",
        "pm_team":    [],
        "pm_assignments": {},
        "pm_uc_meta": {},
        "pm_tasks":   {},
        "analysis_done": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ══════════════════════════════════════════════════════════════════════════
# UI PAGES
# ══════════════════════════════════════════════════════════════════════════

def page_overview():
    ucs   = st.session_state.use_cases
    fmlib = st.session_state.fm_library
    platform = st.session_state.platform
    mod = PLATFORMS[platform]["modifier"]

    if not ucs:
        st.info("📂 Upload a BW extract ZIP to begin analysis.")
        return

    # ── KPI row ─────────────────────────────────────────────────────────
    total_providers = sum(len(uc["providers"]) for uc in ucs)
    total_fms       = len([f for f, v in fmlib.items() if v.get("isCustom")])
    total_hours     = sum(
        estimate_hours(p["complexityLabel"], mod)
        for uc in ucs for p in uc["providers"]
    )
    complexity_counts = {}
    for uc in ucs:
        for p in uc["providers"]:
            lbl = p["complexityLabel"]
            complexity_counts[lbl] = complexity_counts.get(lbl, 0) + 1

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Use Cases",    len(ucs))
    c2.metric("Providers",    total_providers)
    c3.metric("Custom FMs",   total_fms)
    c4.metric("Est. Hours",   f"{total_hours:,}h")
    c5.metric("Very High",    complexity_counts.get("Very High", 0))

    # ── Complexity donut ────────────────────────────────────────────────
    col1, col2 = st.columns([1, 2])
    with col1:
        labels = list(complexity_counts.keys())
        values = list(complexity_counts.values())
        colors = [complexity_color(l) for l in labels]
        fig = go.Figure(go.Pie(labels=labels, values=values,
                               hole=0.55,
                               marker_colors=colors,
                               textinfo="label+percent"))
        fig.update_layout(title="Complexity Distribution", height=300,
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                          font_color="#e2e8f0", showlegend=False, margin=dict(t=40,b=0,l=0,r=0))
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        # Use Case summary table
        rows = []
        for uc in ucs:
            scores = [p["complexity"] for p in uc["providers"]]
            max_score = max(scores) if scores else 0
            hrs = sum(estimate_hours(p["complexityLabel"], mod) for p in uc["providers"])
            rows.append({
                "Use Case":   uc["name"],
                "Providers":  len(uc["providers"]),
                "Complexity": complexity_label(max_score),
                "Score":      max_score,
                "Est. Hours": hrs,
            })
        df = pd.DataFrame(rows).sort_values("Score", ascending=False)
        st.dataframe(df.drop(columns=["Score"]), use_container_width=True,
                     hide_index=True)


def page_fm_library():
    fmlib = st.session_state.fm_library
    if not fmlib:
        st.info("No FM library loaded. Upload an FM dump ZIP.")
        return

    custom = {k: v for k, v in fmlib.items() if v.get("isCustom")}
    standard = {k: v for k, v in fmlib.items() if not v.get("isCustom")}

    st.metric("Total FMs", len(fmlib))
    col1, col2 = st.columns(2)
    col1.metric("Custom (Y_/Z_)", len(custom))
    col2.metric("SAP Standard", len(standard))

    # Search
    search = st.text_input("🔍 Search FM name", "")
    filtered = {k: v for k, v in fmlib.items()
                if search.lower() in k.lower()} if search else fmlib

    rows = []
    for name, fm in filtered.items():
        rows.append({
            "FM Name":    name,
            "Type":       "Custom" if fm.get("isCustom") else "Standard",
            "Lines":      fm.get("lines", 0),
            "Called FMs": len(fm.get("children", [])),
        })
    df = pd.DataFrame(rows).sort_values(["Type", "Lines"], ascending=[True, False])
    st.dataframe(df, use_container_width=True, hide_index=True)

    # FM detail
    selected = st.selectbox("Select FM to inspect", [""] + list(filtered.keys()))
    if selected and selected in filtered:
        fm = filtered[selected]
        st.subheader(f"ƒ {selected}")
        cols = st.columns(3)
        cols[0].metric("Type", "Custom" if fm.get("isCustom") else "SAP Standard")
        cols[1].metric("Lines", fm.get("lines", 0))
        cols[2].metric("Dependencies", len(fm.get("children", [])))
        if fm.get("children"):
            st.write("**Called FMs:**", ", ".join(fm["children"][:20]))
        if fm.get("interface"):
            with st.expander("Interface"):
                st.code(fm["interface"], language="abap")
        if fm.get("source"):
            with st.expander("Source (first 2000 chars)"):
                st.code(fm["source"], language="abap")


def page_complexity():
    ucs = st.session_state.use_cases
    if not ucs:
        st.info("Load data first.")
        return

    platform = st.session_state.platform
    mod = PLATFORMS[platform]["modifier"]

    rows = []
    for uc in ucs:
        for p in uc["providers"]:
            deps = p.get("dependencies", [])
            rows.append({
                "Use Case":      uc["name"],
                "Provider":      p["name"],
                "Complexity":    p["complexityLabel"],
                "Score":         p["complexity"],
                "Est. Hours":    estimate_hours(p["complexityLabel"], mod),
                "Custom FMs":    len({d["name"] for d in deps if d["kind"]=="FM" and re.match(r"^[YZ]", d["name"], re.I)}),
                "Tables":        len([d for d in deps if d["kind"]=="TABLE"]),
                "Has Routines":  any(t.get("hasRoutine") for t in p.get("transformations", [])),
            })

    df = pd.DataFrame(rows).sort_values("Score", ascending=False)

    # Filter by complexity
    complexity_filter = st.multiselect(
        "Filter by complexity",
        ["Very High", "High", "Medium", "Low"],
        default=["Very High", "High", "Medium", "Low"]
    )
    df = df[df["Complexity"].isin(complexity_filter)]

    # Score histogram
    fig = px.histogram(df, x="Score", color="Complexity",
                       color_discrete_map={"Very High":"#ff4466","High":"#ffaa00",
                                           "Medium":"#00d4ff","Low":"#00e676"},
                       title="Complexity Score Distribution")
    fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font_color="#e2e8f0", height=280, margin=dict(t=40,b=0,l=0,r=0))
    st.plotly_chart(fig, use_container_width=True)

    st.dataframe(df, use_container_width=True, hide_index=True)


def page_pm_board(user_email: str):
    ucs = st.session_state.use_cases
    if not ucs:
        st.info("Load BW data first to enable PM planning.")
        return

    platform = st.session_state.platform
    mod = PLATFORMS[platform]["modifier"]

    # ── Team management ─────────────────────────────────────────────────
    st.subheader("👥 Team")
    team = st.session_state.pm_team

    with st.expander("➕ Add Team Member"):
        col1, col2, col3 = st.columns([2, 2, 1])
        new_name = col1.text_input("Name", key="new_member_name")
        new_role = col2.text_input("Role (e.g. BW Architect)", key="new_member_role")
        if col3.button("Add", key="add_member_btn"):
            if new_name and new_role:
                team_id = f"tm_{datetime.now().timestamp()}"
                st.session_state.pm_team.append(
                    {"id": team_id, "name": new_name, "role": new_role}
                )
                st.rerun()

    if team:
        st.dataframe(pd.DataFrame(team)[["name","role"]],
                     use_container_width=True, hide_index=True)

    st.divider()

    # ── Use Case assignments ─────────────────────────────────────────────
    st.subheader("📋 Use Case Assignments")

    team_options = {m["name"]: m["id"] for m in team}
    team_names = ["— None —"] + list(team_options.keys())

    uc_meta = st.session_state.pm_uc_meta

    for uc in ucs:
        scores = [p["complexity"] for p in uc["providers"]]
        max_score = max(scores) if scores else 0
        label = complexity_label(max_score)
        hrs = sum(estimate_hours(p["complexityLabel"], mod) for p in uc["providers"])

        color_map = {"Very High":"🔴","High":"🟡","Medium":"🔵","Low":"🟢"}
        icon = color_map.get(label, "⚪")

        meta = uc_meta.get(uc["name"], {})

        with st.expander(f"{icon} **{uc['name']}** — {label} · {hrs}h · {len(uc['providers'])} providers"):
            col1, col2, col3 = st.columns(3)

            # Lead
            current_lead_id = meta.get("leadId", "")
            current_lead_name = next((m["name"] for m in team if m["id"]==current_lead_id), "— None —")
            lead_sel = col1.selectbox("Lead", team_names,
                                       index=team_names.index(current_lead_name) if current_lead_name in team_names else 0,
                                       key=f"lead_{uc['name']}")

            # Status
            statuses = ["not_started","in_progress","review","done","blocked"]
            status_labels = {"not_started":"⬜ Not Started","in_progress":"🔵 In Progress",
                             "review":"🟡 In Review","done":"✅ Done","blocked":"🔴 Blocked"}
            current_status = meta.get("status","not_started")
            status_sel = col2.selectbox("Status",
                                         [status_labels[s] for s in statuses],
                                         index=statuses.index(current_status) if current_status in statuses else 0,
                                         key=f"status_{uc['name']}")

            # Hours
            est_hours = col3.number_input("Est. Hours", value=meta.get("estHours", hrs),
                                           min_value=0, key=f"hrs_{uc['name']}")

            # Developers (multiselect)
            current_dev_names = [m["name"] for m in team if m["id"] in meta.get("devIds",[])]
            devs_sel = st.multiselect("Developer(s)", list(team_options.keys()),
                                       default=[d for d in current_dev_names if d in team_options],
                                       key=f"devs_{uc['name']}")

            notes = st.text_area("Notes", value=meta.get("notes",""),
                                  key=f"notes_{uc['name']}", height=60)

            if st.button("💾 Save", key=f"save_{uc['name']}"):
                # Resolve IDs
                lead_id = team_options.get(lead_sel, "")
                dev_ids = [team_options[d] for d in devs_sel if d in team_options]
                status_key = [s for s in statuses if status_labels[s]==status_sel][0]

                uc_meta[uc["name"]] = {
                    "leadId": lead_id,
                    "devIds": dev_ids,
                    "estHours": est_hours,
                    "status": status_key,
                    "notes": notes,
                }
                st.session_state.pm_uc_meta = uc_meta
                st.success("Saved ✓")

    # ── Auto-save to Supabase ────────────────────────────────────────────
    st.divider()
    col1, col2 = st.columns(2)
    if col1.button("☁ Save Plan to Cloud"):
        plan = {
            "team":        st.session_state.pm_team,
            "assignments": st.session_state.pm_assignments,
            "ucMeta":      st.session_state.pm_uc_meta,
            "tasks":       st.session_state.pm_tasks,
        }
        if db_save_pm_plan(user_email, plan):
            st.success("Plan saved to cloud ✓")

    if col2.button("⬇ Load Plan from Cloud"):
        plan = db_load_pm_plan(user_email)
        if plan:
            st.session_state.pm_team        = plan.get("team", [])
            st.session_state.pm_assignments = plan.get("assignments", {})
            st.session_state.pm_uc_meta     = plan.get("ucMeta", {})
            st.session_state.pm_tasks       = plan.get("tasks", {})
            st.success("Plan loaded ✓")
            st.rerun()
        else:
            st.warning("No saved plan found.")


def page_admin(user_email: str):
    """Admin usage dashboard — only visible to admin users."""
    admin_emails = st.secrets.get("admin_emails", [])
    if user_email not in admin_emails:
        st.warning("Admin access only.")
        return

    st.subheader("📊 Usage Analytics")
    data = db_get_all_usage()
    if data:
        df = pd.DataFrame(data)
        df["uploaded_at"] = pd.to_datetime(df["uploaded_at"])

        col1, col2, col3 = st.columns(3)
        col1.metric("Total Uploads", len(df))
        col2.metric("Unique Users", df["user_email"].nunique())
        col3.metric("Avg UCs / Upload", round(df["uc_count"].mean(), 1))

        st.write("**Uploads by user:**")
        st.dataframe(df.groupby("user_email")["filename"].count()
                     .reset_index(name="uploads").sort_values("uploads", ascending=False),
                     use_container_width=True, hide_index=True)

        st.write("**All upload events:**")
        st.dataframe(df[["user_email","filename","uc_count","provider_count","uploaded_at"]],
                     use_container_width=True, hide_index=True)
    else:
        st.info("No upload data yet.")


# ══════════════════════════════════════════════════════════════════════════
# MAIN APP
# ══════════════════════════════════════════════════════════════════════════

def main():
    init_session()

    # ── Authentication ─────────────────────────────────────────────────
    user_email = require_login()

    # ── Sidebar ────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown(f"""
        <div style="padding:12px 0 8px;">
          <div style="font-size:18px;font-weight:800;color:#00d4ff;">⚡ BW Migration</div>
          <div style="font-size:10px;color:#475569;margin-top:2px;">Intelligence Platform</div>
        </div>
        """, unsafe_allow_html=True)

        st.caption(f"👤 {user_email}")

        # Platform selector
        platform_names = {k: f"{v['icon']} {v['name']}" for k, v in PLATFORMS.items()}
        selected_platform = st.selectbox(
            "Target Platform",
            list(platform_names.keys()),
            format_func=lambda k: platform_names[k],
            index=list(PLATFORMS.keys()).index(st.session_state.platform)
        )
        st.session_state.platform = selected_platform

        st.divider()

        # ── File upload ────────────────────────────────────────────────
        st.subheader("📂 Load Data")
        uploaded_files = st.file_uploader(
            "Upload BW Extract ZIP(s)",
            type=["zip"],
            accept_multiple_files=True,
            key="zip_uploader",
            help="Upload the ZIP output from your SAP BW extraction scripts"
        )

        if uploaded_files and st.button("▶ Analyse", type="primary"):
            with st.spinner("Parsing ZIP files..."):
                all_ucs = []
                all_fms = {}
                for f in uploaded_files:
                    result = analyse_zip(f.read())
                    all_ucs.extend(result["use_cases"])
                    all_fms.update(result["fm_library"])
                    # Log to Supabase
                    db_log_upload(
                        user_email=user_email,
                        filename=f.name,
                        uc_count=len(result["use_cases"]),
                        provider_count=sum(len(uc["providers"]) for uc in result["use_cases"])
                    )
                # Merge use cases with same name
                merged: dict[str, dict] = {}
                for uc in all_ucs:
                    if uc["name"] in merged:
                        merged[uc["name"]]["providers"].extend(uc["providers"])
                    else:
                        merged[uc["name"]] = uc
                st.session_state.use_cases  = list(merged.values())
                st.session_state.fm_library = all_fms
                st.session_state.analysis_done = True
            st.success(f"✓ {len(st.session_state.use_cases)} use cases, "
                       f"{len(all_fms)} FMs loaded")
            st.rerun()

        # Stats
        if st.session_state.use_cases:
            st.markdown(f"""
            **{len(st.session_state.use_cases)}** use cases  
            **{sum(len(uc['providers']) for uc in st.session_state.use_cases)}** providers  
            **{len(st.session_state.fm_library)}** FMs  
            """)

        st.divider()
        # Upload history
        with st.expander("📋 Your upload history"):
            history = db_get_upload_history(user_email)
            if history:
                for h in history[:10]:
                    st.caption(f"• {h['filename']} — {h['uc_count']} UCs — {h['uploaded_at'][:10]}")
            else:
                st.caption("No uploads yet")

        st.divider()
        page = st.radio(
            "Navigate",
            ["Overview", "FM Library", "Complexity", "PM Board", "✦ AI Assistant", "Admin"],
            key="nav_page"
        )

    # ── Main content ───────────────────────────────────────────────────
    st.title({
        "Overview":         "📊 Overview",
        "FM Library":       "ƒ FM Library",
        "Complexity":       "🎯 Complexity Analysis",
        "PM Board":         "📋 PM Board",
        "✦ AI Assistant":   "✦ AI Migration Assistant",
        "Admin":            "⚙ Admin",
    }.get(page, page))

    if page == "Overview":         page_overview()
    elif page == "FM Library":     page_fm_library()
    elif page == "Complexity":     page_complexity()
    elif page == "PM Board":       page_pm_board(user_email)
    elif page == "✦ AI Assistant": page_ai_assistant()
    elif page == "Admin":          page_admin(user_email)


if __name__ == "__main__":
    main()


# ══════════════════════════════════════════════════════════════════════════
# AI ASSISTANT — Azure OpenAI (GPT-4.1)
# Key is stored in Streamlit secrets only — never in code or GitHub
# ══════════════════════════════════════════════════════════════════════════

@st.cache_resource
def get_openai_client():
    """
    Returns Azure OpenAI client.
    Credentials come from st.secrets — set in Streamlit Cloud dashboard,
    never committed to GitHub.
    """
    try:
        from openai import AzureOpenAI
        return AzureOpenAI(
            azure_endpoint = st.secrets["azure_openai"]["endpoint"],
            api_key        = st.secrets["azure_openai"]["api_key"],
            api_version    = st.secrets["azure_openai"]["api_version"],
        )
    except KeyError:
        return None
    except Exception:
        return None


def build_ai_context(use_cases: list, fm_library: dict, platform: str) -> str:
    """Build a concise summary of the loaded BW data to inject as AI context."""
    if not use_cases:
        return "No BW data has been loaded yet."

    all_providers = [p for uc in use_cases for p in uc["providers"]]
    custom_fms    = [name for name, fm in fm_library.items() if fm.get("isCustom")]
    mod           = PLATFORMS.get(platform, {}).get("modifier", 1.0)
    total_hours   = sum(estimate_hours(p["complexityLabel"], mod) for p in all_providers)

    uc_lines = "\n".join(
        f"- {uc['name']}: {len(uc['providers'])} providers, "
        f"max complexity: {complexity_label(max((p['complexity'] for p in uc['providers']), default=0))}"
        for uc in use_cases
    )
    prov_lines = "\n".join(
        f"- {p['name']}: {p['complexityLabel']} ({p['complexity']} pts), "
        f"{len(p['transformations'])} transformations, "
        f"{len({d['name'] for d in p['dependencies'] if d['kind']=='FM'})} unique FMs"
        for p in all_providers[:40]           # cap to avoid token overflow
    )

    return f"""
USE CASES ({len(use_cases)}):
{uc_lines}

PROVIDERS ({len(all_providers)} total):
{prov_lines}

CUSTOM FMs ({len(custom_fms)}): {', '.join(custom_fms[:60])}

SELECTED TARGET PLATFORM: {PLATFORMS.get(platform, {}).get('name', platform)}
ESTIMATED TOTAL EFFORT: {total_hours:,}h
""".strip()


SYSTEM_PROMPT = """You are a senior enterprise data architect and SAP BW migration expert.
You specialise in migrating SAP BW/4HANA landscapes to modern cloud platforms:
Databricks (Delta Lake), Snowflake, Azure ADF, Azure Synapse, Microsoft Fabric, AWS Glue.

You have been given a real analysis of the customer's SAP BW system (use cases, providers,
custom function modules, complexity scores, effort estimates). Use this context when answering.

Guidelines:
- Be concise and practical — give actionable advice, not generic theory
- When discussing effort, reference the complexity scores and hour estimates provided
- When recommending migration patterns, refer to the specific BW objects in the data
- Format code examples with triple backticks and the language name
- For ABAP → Python/PySpark translations, show both sides
- Flag reuse opportunities when multiple use cases share FMs or DSOs
"""


def page_ai_assistant():
    """AI chat page powered by Azure OpenAI GPT-4.1."""

    client     = get_openai_client()
    use_cases  = st.session_state.use_cases
    fm_library = st.session_state.fm_library
    platform   = st.session_state.platform

    # ── Check AI is configured ──────────────────────────────────────────
    if client is None:
        st.warning(
            "⚙️ Azure OpenAI is not configured. "
            "Add `[azure_openai]` credentials to Streamlit Secrets (see README)."
        )
        with st.expander("How to configure"):
            st.code("""
# In Streamlit Cloud → App Settings → Secrets, add:
[azure_openai]
endpoint   = "https://your-resource.openai.azure.com/"
api_key    = "YOUR_ROTATED_KEY"          # never put the real key here
deployment = "gpt-4.1"
api_version = "2025-01-01-preview"
            """, language="toml")
        return

    # ── Chat history in session state ───────────────────────────────────
    if "ai_messages" not in st.session_state:
        st.session_state.ai_messages = []

    if not use_cases:
        st.info("📂 Load a BW extract ZIP first so the AI can analyse your specific landscape.")
    else:
        st.success(
            f"✅ AI has context: {len(use_cases)} use cases · "
            f"{sum(len(uc['providers']) for uc in use_cases)} providers · "
            f"{len([f for f in fm_library.values() if f.get('isCustom')])} custom FMs"
        )

    # ── Suggested questions ──────────────────────────────────────────────
    if not st.session_state.ai_messages:
        st.markdown("**Suggested questions:**")
        suggestions = [
            "Which use cases should we migrate first and why?",
            "What are the highest-risk custom FMs to rewrite?",
            "How do I convert ABAP start routines to PySpark?",
            "Which DSOs are shared across multiple use cases?",
            "Estimate the team structure needed for this migration.",
        ]
        cols = st.columns(len(suggestions))
        for col, q in zip(cols, suggestions):
            if col.button(q, use_container_width=True):
                st.session_state.ai_messages.append({"role": "user", "content": q})
                st.rerun()

    # ── Display chat history ─────────────────────────────────────────────
    chat_container = st.container(height=450)
    with chat_container:
        for msg in st.session_state.ai_messages:
            with st.chat_message(msg["role"],
                                  avatar="👤" if msg["role"]=="user" else "✦"):
                st.markdown(msg["content"])

    # ── Input bar ────────────────────────────────────────────────────────
    user_input = st.chat_input(
        "Ask about your BW landscape, migration strategy, effort estimates…"
    )

    if user_input:
        # Add user message
        st.session_state.ai_messages.append({"role": "user", "content": user_input})

        # Build messages for API call
        context    = build_ai_context(use_cases, fm_library, platform)
        system_msg = SYSTEM_PROMPT + f"\n\nBW LANDSCAPE CONTEXT:\n{context}"

        api_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in st.session_state.ai_messages[-12:]  # keep last 12 turns
        ]

        # Call Azure OpenAI
        with st.spinner("Analysing your BW landscape…"):
            try:
                deployment = st.secrets["azure_openai"]["deployment"]
                response = client.chat.completions.create(
                    model    = deployment,          # "gpt-4.1"
                    messages = [
                        {"role": "system", "content": system_msg},
                        *api_messages,
                    ],
                    max_tokens  = 1200,
                    temperature = 0.3,
                )
                reply = response.choices[0].message.content
            except Exception as e:
                reply = f"❌ AI error: {e}"

        st.session_state.ai_messages.append({"role": "assistant", "content": reply})
        st.rerun()

    # ── Clear chat ───────────────────────────────────────────────────────
    if st.session_state.ai_messages:
        if st.button("🗑 Clear conversation"):
            st.session_state.ai_messages = []
            st.rerun()
