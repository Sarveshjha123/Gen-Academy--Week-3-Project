"""MORNING PROCUREMENT DESK (Streamlit).      Run:  streamlit run app.py
Two users (Plant Heads). Each opens on their TOP 5 tasks (red first). "Work on it" runs the task's playbook: sub-agents
gather facts, the drafter + critic write the email, and the app PAUSES for approval. Approve -> sent, task leaves the list,
the next one moves up. Reject -> the policy-advisor recommends a next step and you choose.
"""
import shutil

import streamlit as st

from src import config
from src.agent.systems import run_sync
from src.core import tasks as T

st.set_page_config(page_title="Morning Procurement Desk", page_icon="🏭", layout="wide")
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@600;700&display=swap');
:root { --ink:#1E2329; --muted:#5E6873; --line:#DDE1E5; --paper:#F4F5F6; --red:#B3402F; --red-soft:#F9E6E2; --amber:#C7781A;
        --amber-soft:#FBF0E1; --green:#2D7A52; --green-soft:#E3F1E9; --low:#6B7785; --low-soft:#EEF0F2; --blue-soft:#E4EDF5; }
html, body, [class*="css"], p, li, td, th, .stMarkdown { font-family:'IBM Plex Sans', sans-serif !important; color:var(--ink); }
.stApp { background:var(--paper); }
.block-container { max-width:1180px; padding-top:4rem; padding-bottom:6rem; }
h1,h2,h3,h4,.title { font-family:'IBM Plex Sans Condensed', sans-serif !important; }
.title { font-size:2rem; font-weight:700; margin:0; line-height:1.25; }
.sub { color:var(--muted); margin:.2rem 0 1rem; }
.kpis { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:10px; margin-bottom:16px; }
.kpi { background:#fff; border:1px solid var(--line); border-radius:10px; padding:10px 14px; }
.kpi b { display:block; font-family:'IBM Plex Sans Condensed',sans-serif; font-size:1.6rem; line-height:1.1; }
.kpi span { color:var(--muted); font-size:.82rem; }
.card { background:#fff; border:1px solid var(--line); border-left:6px solid var(--low); border-radius:10px; padding:11px 16px; margin-bottom:8px; }
.card.red { border-left-color:var(--red); } .card.orange { border-left-color:var(--amber); }
.badge { display:inline-block; font-size:.72rem; font-weight:600; padding:1px 8px; border-radius:99px; margin-right:8px; letter-spacing:.03em; }
.badge.red { background:var(--red-soft); color:var(--red); } .badge.orange { background:var(--amber-soft); color:var(--amber); }
.badge.low { background:var(--low-soft); color:var(--low); }
.card .t { font-weight:600; } .card .w { color:var(--muted); font-size:.9rem; margin-top:2px; }
.panel { background:#fff; border:2px solid var(--amber); border-radius:12px; padding:14px 18px; margin:10px 0; }
.done { background:var(--green-soft); border:1px solid #BFDCCB; border-radius:12px; padding:12px 16px; margin:10px 0; }
div.stButton > button { border-radius:8px; font-size:.86rem; }
@media (max-width:800px){ .kpis { grid-template-columns:repeat(2, minmax(0,1fr)); } }
</style>""", unsafe_allow_html=True)


def tidy(text):
    return str(text or "").replace("$", "\\$")


@st.cache_resource(show_spinner="Starting the sub-agents and the two MCP servers (first time ~30 s)...")
def get_ctx(plant_id):
    from src.agent.subagents import build_context
    return run_sync(build_context(plant_id))


@st.cache_resource(show_spinner=False)
def get_runner(plant_id):
    from src.agent.task_runner import build_runner
    return build_runner(get_ctx(plant_id))


@st.cache_resource(show_spinner=False)
def get_supervisor(plant_id):
    from src.agent.supervisor import build_supervisor
    return build_supervisor(get_ctx(plant_id))


st.session_state.setdefault("plant", "AUS")
st.session_state.setdefault("run", None)          # the task being worked on: {task_id, thread, pending|result}
st.session_state.setdefault("chat", [])

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.markdown('<p class="title" style="font-size:1.3rem">🏭 Procurement Desk</p>', unsafe_allow_html=True)
    st.caption(f"Northfield Industrial (fictional) · {config.TODAY:%A %b %d, %Y}")
    labels = {"AUS": "Dana Whitfield · Austin Plant Head", "DAL": "Marcus Bell · Dallas Plant Head"}
    who = st.radio("Signed in as", list(labels), format_func=labels.get, index=list(labels).index(st.session_state.plant))
    if who != st.session_state.plant:
        st.session_state.update(plant=who, run=None, chat=[])
        st.rerun()
    st.divider()
    st.caption("Approvals: ≤ \\$25k Junior Procurement Lead · \\$25k–\\$100k Procurement Head · > \\$100k + Finance Head")
    if st.button("↺ Reset demo (both queues, drafts, outbox)", use_container_width=True):
        for sub in ("drafts", "outbox", "runs", "research", "tasks"):
            shutil.rmtree(config.WORKSPACE / sub, ignore_errors=True)
            (config.WORKSPACE / sub).mkdir(parents=True, exist_ok=True)
        T.reset()
        st.session_state.update(run=None, chat=[])
        st.rerun()
    st.caption(f"Models: {config.CHAT_MODEL} (supervisor) · {config.HELPER_MODEL} (sub-agents) · critic {config.JUDGE_MODEL}")

plant_id = st.session_state.plant
name = T.USERS[plant_id]
all_tasks = T.load(plant_id)
open_ = T.open_tasks(plant_id)
fin = T.finished(plant_id)

st.markdown(f'<p class="title">Good morning, {name.split()[0]}</p>', unsafe_allow_html=True)
st.markdown(f'<p class="sub">{"Austin" if plant_id == "AUS" else "Dallas"} plant · your top 5 for today. '
            'Finish one and the next moves up.</p>', unsafe_allow_html=True)
cnt = {p: sum(t["priority"] == p for t in open_) for p in ("red", "orange", "low")}
sent = len(list((config.WORKSPACE / "outbox").glob(f"{plant_id}-*_email.txt"))) if (config.WORKSPACE / "outbox").exists() else 0
st.markdown(f"""<div class="kpis"><div class="kpi"><b style="color:var(--red)">{cnt['red']}</b><span>red open</span></div>
<div class="kpi"><b style="color:var(--amber)">{cnt['orange']}</b><span>orange open</span></div>
<div class="kpi"><b>{cnt['low']}</b><span>low open</span></div>
<div class="kpi"><b style="color:var(--green)">{len(fin)}</b><span>finished today · {sent} emails sent</span></div></div>""",
            unsafe_allow_html=True)


def start_task(task_id):
    from src.agent import task_runner
    with st.spinner("Working on it: sub-agents are gathering facts and drafting (30-90 s)..."):
        thread, out = run_sync(task_runner.start(get_runner(plant_id), task_id))
    st.session_state.run = {"task_id": task_id, "thread": thread, **out}


def answer(value, msg="Working..."):
    from src.agent import task_runner
    r = st.session_state.run
    with st.spinner(msg):
        out = run_sync(task_runner.resume(get_runner(plant_id), r["thread"], value))
    st.session_state.run = {"task_id": r["task_id"], "thread": r["thread"], **out}
    st.rerun()


def show_trace(trace):
    if trace:
        st.caption("Helpers used: " + " → ".join(f"{t['agent']}" + (f" ({t['seconds']}s)" if t.get("seconds") else "")
                                                 for t in trace if t["agent"] != "code"))


# ---------------------------------------------------------------- the task being worked on
run = st.session_state.run
if run:
    task = next((t for t in all_tasks if t["id"] == run["task_id"]), {"title": run["task_id"], "priority": "low"})
    st.markdown(f"#### ▶ {task['title']}")
    pend = run.get("pending")
    if pend and pend.get("type") == "approval":
        e = pend["email"]
        st.markdown(f'<div class="panel"><b>✋ Your OK is needed before anything is sent.</b> Draft version {pend["version"]} · '
                    f'to <b>{tidy(pend["recipient_role"])}</b>{(" · " + tidy(pend["amount"])) if pend.get("amount") else ""}</div>',
                    unsafe_allow_html=True)
        with st.expander("What the sub-agents found", expanded=pend["version"] == 1):
            st.markdown(tidy(pend["summary_md"]))
            show_trace(pend.get("trace"))
        crit = pend.get("critic") or {}
        if pend.get("feedback"):
            st.caption(f"✏️ Version {pend['version']} includes your change: “{pend['feedback']}”")
        if crit.get("first_draft_issues"):
            st.caption("🧐 Critic on the first draft: " + "; ".join(crit["first_draft_issues"]) + (" → fixed" if crit.get("fixed") else ""))
        elif crit:
            st.caption("🧐 Critic reviewed the draft: no issues.")
        k = f"{run['thread']}_{pend['key']}_{pend['version']}"
        c1, c2 = st.columns([1, 1])
        with c1:
            st.markdown(f"**From:** {e['from']}")
            to = st.text_input("To", e["to"], key=f"to{k}")
            subj = st.text_input("Subject", e["subject"], key=f"su{k}")
            body = st.text_area("Email", e["body"], height=330, key=f"bo{k}")
        with c2:
            form = config.WORKSPACE / "drafts" / (pend.get("form_file") or "")
            if pend.get("form_file") and form.exists():
                st.markdown(f"**Attached: {form.name}**")
                st.markdown(tidy(form.read_text()))
            else:
                st.markdown(f"**Purpose:** {tidy(pend['purpose'])}")
        edited = (to, subj, body) != (e["to"], e["subject"], e["body"])
        b1, b2 = st.columns([1, 3])
        if b1.button("✅ Approve and send", type="primary", key=f"ok{k}"):
            answer({"decision": "edit" if edited else "approve", "email": {"to": to, "subject": subj, "body": body}}, "Sending...")
        b2.caption("Edited the text yourself? Approve sends your version.")
        r1, r2 = st.columns([3, 1])
        change = r1.text_input("Not happy? Tell the drafter what to change or add", key=f"ch{k}",
                               placeholder="e.g. Add that delivery must arrive before Nov 5, and make it shorter")
        if r2.button("✏️ Request changes", key=f"rv{k}", disabled=not change.strip()):
            answer({"decision": "revise", "message": change.strip()}, "The drafter is rewriting...")
        x1, x2 = st.columns([3, 1])
        why = x1.text_input("Reason for rejecting (the policy advisor uses it)", key=f"wy{k}")
        if x2.button("❌ Reject", key=f"no{k}"):
            answer({"decision": "reject", "message": why or "Not approved."}, "The policy advisor is checking what to do next...")
    elif pend and pend.get("type") == "choice":
        st.markdown(f'<div class="panel"><b>{tidy(pend.get("question", "What next?"))}</b></div>', unsafe_allow_html=True)
        st.markdown(tidy(pend["summary_md"]))
        show_trace(pend.get("trace"))
        for o in pend["options"]:
            label = ("⭐ " if o.get("recommended") else "") + o["label"]
            kind = "primary" if o.get("recommended") else "secondary"
            if o.get("input") == "qty":
                q1, q2 = st.columns([1, 3])
                val = q1.number_input("New quantity", min_value=1, step=1, value=int(o.get("default") or 1),
                                      key=f"in{run['thread']}{o['id']}", label_visibility="collapsed")
                if q2.button(label, key=f"op{run['thread']}{o['id']}", type=kind):
                    answer({"choice": o["id"], "value": int(val)}, "Redrafting with your quantity...")
            elif st.button(label, key=f"op{run['thread']}{o['id']}", type=kind):
                answer({"choice": o["id"]})
        st.caption("⭐ = recommended by the policy advisor" if any(o.get("recommended") for o in pend["options"]) else "")
    else:
        res = run.get("result", {})
        icon = {"done": "✅", "closed": "☑️", "snoozed": "💤"}.get(res.get("status"), "ℹ️")
        fu = [t for t in T.load(plant_id) + T.load("DAL" if plant_id == "AUS" else "AUS") if t["id"] in (res.get("followups") or [])]
        st.markdown(f'<div class="done">{icon} <b>{tidy(res.get("outcome", "Finished."))}</b>'
                    + (f'<br>Sent: {res["sent"]}' if res.get("sent") else "")
                    + "".join(f'<br>➕ New task for {T.USERS[t["plant_id"]]}: <b>{tidy(t["title"])}</b> ({t["priority"]})' for t in fu)
                    + "</div>", unsafe_allow_html=True)
        show_trace(res.get("trace"))
        if st.button("Back to my list", type="primary"):
            st.session_state.run = None
            st.rerun()
    st.divider()

# ---------------------------------------------------------------- top 5
st.markdown("#### Your top 5 today")
busy = bool(run and (run.get("pending")))
for t in T.top5(plant_id):
    c1, c2 = st.columns([5, 1])
    with c1:
        st.markdown(f'<div class="card {t["priority"]}"><span class="badge {t["priority"]}">{t["priority"].upper()}</span>'
                    f'<span class="t">{tidy(t["title"])}</span><div class="w">{tidy(t["why"])}</div></div>', unsafe_allow_html=True)
    with c2:
        if st.button("▶ Work on it", key=f"go{t['id']}", use_container_width=True, disabled=busy):
            start_task(t["id"])
            st.rerun()
rest = open_[5:]
if rest:
    with st.expander(f"Next up ({len(rest)} more)"):
        for t in rest:
            st.markdown(f'<span class="badge {t["priority"]}">{t["priority"].upper()}</span>{tidy(t["title"])}', unsafe_allow_html=True)
if fin:
    with st.expander(f"Finished ({len(fin)})"):
        for t in fin:
            st.markdown(f"- **{tidy(t['title'])}** · {t['status']}: {tidy(t.get('outcome', ''))}")

# ---------------------------------------------------------------- chat
st.divider()
st.markdown("#### Ask the desk")
st.caption("Questions only (sending happens from the task cards). Try: *We're low on hydraulic pumps, what are my options?* · "
           "*Order 80 P-110* · *Does another plant have spare P-110?* · *When will my open order for P-110 arrive?* · "
           "*What does Juniper's contract say about late delivery?* · *Who approves a \\$150,000 order?* · "
           "*Can we do an emergency purchase?*")
for m in st.session_state.chat:
    with st.chat_message(m["role"], avatar="🧑‍🏭" if m["role"] == "user" else "🏭"):
        st.markdown(tidy(m["content"]))
        if m.get("info"):
            i = m["info"]
            subs = sorted({x.split(":", 1)[1] for x in i["tools_called"] if x.startswith("task:")})
            v = i.get("verification") or {}
            if (i.get("scope") or {}).get("scope") == "out":
                badge = "🚫 outside this desk's scope: no tools used"
            elif v.get("unsupported"):
                badge = "⚠️ not found in any tool result, double-check: " + ", ".join(v["unsupported"])
            elif v.get("checked"):
                badge = f"✅ {v['checked']} numbers, dates and IDs checked against tool results" + (" (after one correction)" if v.get("repaired") else "")
            else:
                badge = ""
            if badge:
                st.caption(tidy(badge))
            st.caption(f"⏱ {i['latency_s']} s · {i['llm_calls']} model calls" + (f" · sub-agents: {', '.join(subs)}" if subs else ""))
q = st.chat_input("Ask about stock, vendors, contracts, policy...")
if q:
    from src.agent.supervisor import ask, new_thread
    st.session_state.setdefault("chat_thread", new_thread())
    st.session_state.chat.append({"role": "user", "content": q})
    with st.spinner("Planning and asking the sub-agents..."):
        r = run_sync(ask(get_supervisor(plant_id), st.session_state.chat_thread, q))
    st.session_state.chat.append({"role": "assistant", "content": r["answer"], "info": r})
    st.rerun()

# ---------------------------------------------------------------- shared folder (like the Deep Agents demo)
st.divider()
st.markdown("#### 📁 Shared folder")
cols = st.columns(3)
for col, sub, label in zip(cols, ("drafts", "outbox", "research"), ("Drafts (every version)", "Outbox (sent after your OK)",
                                                                    "Research (sub-agent findings)")):
    folder = config.WORKSPACE / sub
    files = sorted([f for f in folder.glob("*") if f.is_file() and not f.name.startswith((".", "pending_"))],
                   key=lambda f: f.stat().st_mtime, reverse=True) if folder.exists() else []
    with col:
        with st.expander(f"{label}: {len(files)}"):
            for f in files[:30]:
                with st.popover(f.name, use_container_width=True):
                    st.text(f.read_text()[:6000])
