import os
import sys
import json
import re
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, send_file, render_template_string

# Load .env if present
def load_dotenv_simple():
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k not in os.environ:
                        os.environ[k] = v
load_dotenv_simple()

# Environment Configuration
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "gemma-4-26b-a4b-it")
LLM_MODEL_STRONG = os.environ.get("LLM_MODEL_STRONG", "gemma-4-31b-it")
THINKING_LEVEL = os.environ.get("THINKING_LEVEL", "minimal")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gemini")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
SAT_TARGET = int(os.environ.get("SAT_TARGET", 4))
MAX_REVISIONS = int(os.environ.get("MAX_REVISIONS", 2))
TOKEN_BUDGET = int(os.environ.get("TOKEN_BUDGET", 25000))
SESSION_TTL = 6 * 3600

ROOM_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
STAGE_NAMES = [
    "1. Proposals",
    "2. Disagreement check",
    "3. Critique",
    "4. Verdict",
    "5. Satisfaction",
    "6. Revision",
    "Baseline (1 call)",
    "Baseline ratings"
]

app = Flask(__name__, static_folder=None)
session_lock = threading.RLock()
SESSIONS = {}

def clean_expired_sessions():
    now = time.time()
    with session_lock:
        expired = [sid for sid, s in SESSIONS.items() if now - s.get("created_at", now) > SESSION_TTL]
        for sid in expired:
            del SESSIONS[sid]

def generate_room_code():
    clean_expired_sessions()
    with session_lock:
        for _ in range(100):
            code = "".join(random.choices(ROOM_CHARS, k=4))
            if code not in SESSIONS:
                return code
        # Fallback 5 chars if dense
        return "".join(random.choices(ROOM_CHARS, k=5))

def extract_json(text):
    if not text:
        return None
    text_clean = text.strip()
    match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', text_clean)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            pass
    try:
        return json.loads(text_clean)
    except Exception:
        return None

# LLM Core Engine
def llm(prompt, max_tokens, temperature, model=None):
    if model is None:
        model = LLM_MODEL
    
    import requests
    
    if LLM_PROVIDER.lower() == "openai":
        url = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}"
        }
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature
        }
        resp = requests.post(url, json=body, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = data.get("usage", {})
        in_tok = usage.get("prompt_tokens", len(prompt) // 4)
        out_tok = usage.get("completion_tokens", len(text) // 4)
        return text, {"in": in_tok, "out": out_tok}
    
    # Gemini Provider (Default)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={LLM_API_KEY}"
    headers = {"Content-Type": "application/json"}
    
    gen_config = {
        "maxOutputTokens": max_tokens,
        "temperature": temperature
    }
    if THINKING_LEVEL != "":
        gen_config["thinkingConfig"] = {"thinkingLevel": THINKING_LEVEL}
        
    body = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "generationConfig": gen_config
    }
    
    data = None
    for attempt in range(3):
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=60)
            if resp.status_code in [503, 429] and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            if attempt < 2 and ("503" in str(e) or "429" in str(e)):
                time.sleep(1.5 * (attempt + 1))
                continue
            # If strong model fails with 503 or quota, fallback gracefully to fast model
            if model == LLM_MODEL_STRONG and model != LLM_MODEL:
                print(f"Warning: {model} unavailable, falling back to {LLM_MODEL}", file=sys.stderr)
                return llm(prompt, max_tokens, temperature, model=LLM_MODEL)
            raise e
    
    if data is None:
        raise RuntimeError("No response data received from model API")

    parts = []
    candidates = data.get("candidates", [])
    if candidates:
        content_parts = candidates[0].get("content", {}).get("parts", [])
        for p in content_parts:
            # Exclude parts marked thought: true
            if not p.get("thought", False):
                parts.append(p.get("text", ""))
    text = "".join(parts).strip()
    
    usage = data.get("usageMetadata", {})
    in_tok = usage.get("promptTokenCount")
    cand_tok = usage.get("candidatesTokenCount", 0)
    thought_tok = usage.get("thoughtsTokenCount", 0)
    
    if in_tok is None:
        in_tok = len(prompt) // 4
    if cand_tok == 0 and thought_tok == 0:
        out_tok = len(text) // 4
    else:
        out_tok = cand_tok + thought_tok
        
    return text, {"in": in_tok, "out": out_tok}

def call(session, stage, prompt, max_tokens, temperature, strong=False):
    model = LLM_MODEL_STRONG if strong else LLM_MODEL
    text, usage = llm(prompt, max_tokens, temperature, model=model)
    with session_lock:
        if stage not in session["usage"]:
            session["usage"][stage] = {"calls": 0, "in": 0, "out": 0}
        session["usage"][stage]["calls"] += 1
        session["usage"][stage]["in"] += usage.get("in", 0)
        session["usage"][stage]["out"] += usage.get("out", 0)
    return text

def build_agent_prefix(agent, mode):
    name = agent["name"]
    profile = agent.get("profile", "")
    if mode == "group":
        return f"You are {name}'s personal AI. Represent ONLY {name}'s interests. Private notes from {name}: {profile}. Share only what you need to argue the case; do not recite the private notes word for word."
    else:
        return f"You are {name}, one voice in a council advising a single person. Your personality: {profile}."

# Pipeline Execution in Background Thread
def run_pipeline(session_id):
    try:
        with session_lock:
            session = SESSIONS.get(session_id)
            if not session:
                return
            session["status"] = "running"
            topic = session["topic"]
            mode = session["mode"]
            agents = list(session["agents"])
            want_baseline = session.get("want_baseline", True)

        # 1. Proposals (in parallel, ThreadPoolExecutor, fast model)
        with session_lock:
            session["stage"] = "1. Proposals"

        def get_proposal(ag):
            prefix = build_agent_prefix(ag, mode)
            prompt = (
                f"{prefix}\n\n"
                f"Topic for decision: {topic}\n\n"
                f"Formulate your position. Return ONLY a single JSON object with this exact structure:\n"
                f'{{"position": "<your stance, <=20 words>", "points": ["<=3 key arguments, <=15 words each"], "risks": ["<=2 key risks/objections, <=15 words each"]}}\n'
                f"Ensure the output is valid JSON."
            )
            raw_text = call(session, "1. Proposals", prompt, max_tokens=250, temperature=0.3, strong=False)
            parsed = extract_json(raw_text)
            if isinstance(parsed, dict) and "position" in parsed:
                position = str(parsed.get("position", "")).strip()
                points = [str(p).strip() for p in parsed.get("points", []) if p][:3]
                risks = [str(r).strip() for r in parsed.get("risks", []) if r][:2]
                return ag["name"], {"position": position, "points": points, "risks": risks}
            else:
                return ag["name"], {"position": raw_text[:120].strip(), "points": [], "risks": []}

        with ThreadPoolExecutor(max_workers=min(len(agents), 8)) as executor:
            proposals = dict(executor.map(get_proposal, agents))

        with session_lock:
            session["claims"] = proposals

        # 2. Disagreement Check (fast model, temperature 0, max_tokens 60)
        with session_lock:
            session["stage"] = "2. Disagreement check"

        positions_summary = "\n".join([f"- {name}: {c['position']}" for name, c in proposals.items()])
        check_prompt = (
            f"Topic: {topic}\n\n"
            f"Here are the one-line positions from the participants:\n"
            f"{positions_summary}\n\n"
            f"Determine if there is a fundamental disagreement or trade-off between these positions.\n"
            f"Answer EXACTLY either 'AGREE' (if all are harmonious and compatible) or 'DISAGREE: <main disputed point, under 15 words>'."
        )
        check_raw = call(session, "2. Disagreement check", check_prompt, max_tokens=60, temperature=0.0, strong=False).strip()
        
        has_disagreement = False
        dispute_text = ""
        if check_raw.upper().startswith("DISAGREE"):
            has_disagreement = True
            parts = check_raw.split(":", 1)
            dispute_text = parts[1].strip() if len(parts) > 1 else check_raw
        elif "DISAGREE" in check_raw.upper():
            has_disagreement = True
            dispute_text = check_raw.replace("DISAGREE", "").strip(": ").strip()
        else:
            has_disagreement = False

        with session_lock:
            session["check"] = {
                "agree": not has_disagreement,
                "dispute": dispute_text,
                "raw": check_raw
            }

        # 3. Critique (Only if DISAGREE, in parallel, fast model)
        critiques_list = []
        if has_disagreement:
            with session_lock:
                session["stage"] = "3. Critique"

            def get_critique(ag):
                prefix = build_agent_prefix(ag, mode)
                other_claims = "\n".join([
                    f"- {name}: Position: {c['position']}. Points: {'; '.join(c['points'])}. Risks: {'; '.join(c['risks'])}"
                    for name, c in proposals.items() if name != ag["name"]
                ])
                fix_instruction = "In group mode 'fix' must be a concrete compromise." if mode == "group" else "'fix' is a proposed resolution."
                prompt = (
                    f"{prefix}\n\n"
                    f"Topic: {topic}\n"
                    f"Disputed core point: {dispute_text}\n"
                    f"Other agents' compact claims:\n{other_claims}\n\n"
                    f"Critique other positions to defend your interests. {fix_instruction}\n"
                    f"Return ONLY a JSON array of at most 3 items with exact format:\n"
                    f'[{{"target": "<target agent name>", "flaw": "<flaw in their stance, <=20 words>", "fix": "<compromise/fix, <=20 words>"}}]'
                )
                raw_c = call(session, "3. Critique", prompt, max_tokens=250, temperature=0.3, strong=False)
                parsed = extract_json(raw_c)
                items = []
                if isinstance(parsed, list):
                    items = parsed
                elif isinstance(parsed, dict):
                    for v in parsed.values():
                        if isinstance(v, list):
                            items = v
                            break
                clean_items = []
                for it in items[:3]:
                    if isinstance(it, dict) and "target" in it:
                        clean_items.append({
                            "author": ag["name"],
                            "target": str(it.get("target", "")),
                            "flaw": str(it.get("flaw", ""))[:120],
                            "fix": str(it.get("fix", ""))[:120]
                        })
                return clean_items

            with ThreadPoolExecutor(max_workers=min(len(agents), 8)) as executor:
                for c_res in executor.map(get_critique, agents):
                    critiques_list.extend(c_res)

        with session_lock:
            session["critiques"] = critiques_list

        # 4. Verdict (STRONG model, max_tokens 350, temperature 0.4)
        with session_lock:
            session["stage"] = "4. Verdict"

        claims_digest = "\n".join([
            f"- {name}: {c['position']} (Points: {', '.join(c['points'])}; Risks: {', '.join(c['risks'])})"
            for name, c in proposals.items()
        ])
        critiques_digest = "\n".join([
            f"- {c['author']} to {c['target']}: Flaw='{c['flaw']}' -> Compromise='{c['fix']}'"
            for c in critiques_list
        ]) if critiques_list else "None (All parties in alignment)."

        verdict_prompt = (
            f"You are a neutral moderator deciding the council's outcome on: '{topic}'.\n\n"
            f"Participant compact claims:\n{claims_digest}\n\n"
            f"Critiques and proposed compromises:\n{critiques_digest}\n\n"
            f"Synthesize the input and make a definitive decision. Balance the trade-offs.\n"
            f"Output in EXACTLY this structure with nothing before or after:\n"
            f"DECISION: <clear, definitive group decision>\n"
            f"WHY: <two sentences explaining the rationale and why this is the best balanced outcome>\n"
            f"TRADE-OFFS: <what is given up and by whom>\n"
            f"FIRST STEP: <one concrete action to take today>"
        )
        verdict_text = call(session, "4. Verdict", verdict_prompt, max_tokens=350, temperature=0.4, strong=True).strip()

        # 5. Satisfaction Check (fast model, temperature 0, max_tokens 80)
        def rate_outcome(ag, current_v, stage_label):
            prefix = build_agent_prefix(ag, mode)
            prompt = (
                f"{prefix}\n\n"
                f"Topic: {topic}\n"
                f"Proposed Verdict:\n{current_v}\n\n"
                f"Rate how well this outcome satisfies your interests/person on a scale of 1 to 5.\n"
                f"Be completely honest and critical, not polite. A score of 1 means unacceptable/dealbreaker violated; 5 means ideal.\n"
                f'Return ONLY a JSON object: {{"score": <integer 1-5>, "reason": "<brief justification, <=20 words>"}}'
            )
            raw_r = call(session, stage_label, prompt, max_tokens=80, temperature=0.0, strong=False)
            parsed = extract_json(raw_r)
            score = 3
            reason = "Standard compromise"
            if isinstance(parsed, dict) and "score" in parsed:
                try:
                    score = int(parsed["score"])
                except Exception:
                    score = 3
                reason = str(parsed.get("reason", ""))[:120].strip()
            else:
                m = re.search(r'\b([1-5])\b', raw_r)
                if m:
                    score = int(m.group(1))
                    reason = raw_r[:60].strip()
            score = max(1, min(5, score))
            return ag["name"], {"score": score, "reason": reason}

        with session_lock:
            session["stage"] = "5. Satisfaction"

        with ThreadPoolExecutor(max_workers=min(len(agents), 8)) as executor:
            r1_ratings = dict(executor.map(lambda a: rate_outcome(a, verdict_text, "5. Satisfaction"), agents))

        with session_lock:
            session["rounds"].append({
                "verdict": verdict_text,
                "ratings": r1_ratings
            })

        # 6. Revision Loop
        current_verdict = verdict_text
        current_ratings = r1_ratings
        revisions_done = 0

        while any(r["score"] < SAT_TARGET for r in current_ratings.values()):
            with session_lock:
                council_tok = sum(
                    s["in"] + s["out"]
                    for k, s in session["usage"].items()
                    if not k.startswith("Baseline")
                )
            if revisions_done >= MAX_REVISIONS:
                with session_lock:
                    session["stop_reason"] = "Revision limit reached"
                break
            if council_tok > TOKEN_BUDGET:
                with session_lock:
                    session["stop_reason"] = "Token budget reached"
                break

            revisions_done += 1
            with session_lock:
                session["stage"] = f"6. Revision (Round {revisions_done + 1})"

            objections = "\n".join([
                f"- {name} (Score: {r['score']}/5): {r['reason']}"
                for name, r in current_ratings.items()
            ])

            rev_prompt = (
                f"You are the neutral moderator revising the council's decision on: '{topic}'.\n\n"
                f"Previous outcome:\n{current_verdict}\n\n"
                f"Specific objections and ratings from participants:\n{objections}\n\n"
                f"Revise the outcome to directly address the dissatisfied participants' dealbreakers while preserving essential compromises.\n"
                f"Output in EXACTLY the same structure:\n"
                f"DECISION: <clear, revised decision>\n"
                f"WHY: <two sentences explaining the rationale and how concerns were addressed>\n"
                f"TRADE-OFFS: <what is given up and by whom in this revision>\n"
                f"FIRST STEP: <one concrete action to take today>"
            )
            current_verdict = call(session, "6. Revision", rev_prompt, max_tokens=350, temperature=0.4, strong=True).strip()

            with session_lock:
                session["stage"] = f"5. Satisfaction (Round {revisions_done + 1})"

            with ThreadPoolExecutor(max_workers=min(len(agents), 8)) as executor:
                current_ratings = dict(executor.map(lambda a: rate_outcome(a, current_verdict, "5. Satisfaction"), agents))

            with session_lock:
                session["rounds"].append({
                    "verdict": current_verdict,
                    "ratings": current_ratings
                })

        with session_lock:
            if not session.get("stop_reason"):
                session["stop_reason"] = f"Everyone rated the outcome {SAT_TARGET}/5 or higher"

        # 7. Optional Baseline
        if want_baseline:
            with session_lock:
                session["stage"] = "Baseline (1 call)"

            if mode == "group":
                notes_block = "\n".join([f"- {a['name']}'s private notes: {a.get('profile', '')}" for a in agents])
                baseline_prompt = (
                    f"Decide the best outcome for the group topic: '{topic}'.\n\n"
                    f"Here are the private notes from each participant:\n{notes_block}\n\n"
                    f"Make a direct decision balancing all constraints in EXACTLY this structure:\n"
                    f"DECISION: <definitive choice>\n"
                    f"WHY: <two sentences explaining why>\n"
                    f"TRADE-OFFS: <what is given up>\n"
                    f"FIRST STEP: <one action to take today>"
                )
            else:
                personas_block = "\n".join([f"- {a['name']}: {a.get('profile', '')}" for a in agents])
                baseline_prompt = (
                    f"Decide the best outcome for the council advising on: '{topic}'.\n\n"
                    f"The council consists of these perspectives:\n{personas_block}\n\n"
                    f"Make a direct decision in EXACTLY this structure:\n"
                    f"DECISION: <definitive choice>\n"
                    f"WHY: <two sentences explaining why>\n"
                    f"TRADE-OFFS: <what is given up>\n"
                    f"FIRST STEP: <one action to take today>"
                )

            baseline_text = call(session, "Baseline (1 call)", baseline_prompt, max_tokens=350, temperature=0.4, strong=True).strip()

            with session_lock:
                session["stage"] = "Baseline ratings"

            with ThreadPoolExecutor(max_workers=min(len(agents), 8)) as executor:
                baseline_ratings = dict(executor.map(lambda a: rate_outcome(a, baseline_text, "Baseline ratings"), agents))

            with session_lock:
                session["baseline"] = {
                    "text": baseline_text,
                    "ratings": baseline_ratings
                }

        with session_lock:
            session["status"] = "done"
            session["stage"] = "Complete"

    except Exception as e:
        with session_lock:
            session = SESSIONS.get(session_id)
            if session:
                session["status"] = "error"
                session["error"] = str(e)
                session["stage"] = "Error"
        print(f"Error in pipeline for session {session_id}: {e}", file=sys.stderr)

# Public State Helper
def get_public_session(session):
    with session_lock:
        is_group = session["mode"] == "group"
        if is_group:
            # PRIVACY RULE: in group mode public state must NEVER include any agent's profile!
            public_agents = [{"name": a["name"]} for a in session["agents"]]
        else:
            public_agents = [{"name": a["name"], "profile": a.get("profile", "")} for a in session["agents"]]

        council_tokens = sum(
            s["in"] + s["out"]
            for k, s in session.get("usage", {}).items()
            if not k.startswith("Baseline")
        )

        return {
            "id": session["id"],
            "mode": session["mode"],
            "topic": session["topic"],
            "status": session["status"],
            "stage": session["stage"],
            "error": session.get("error"),
            "agents": public_agents,
            "claims": session.get("claims", {}),
            "check": session.get("check"),
            "critiques": session.get("critiques", []),
            "rounds": session.get("rounds", []),
            "stop_reason": session.get("stop_reason"),
            "baseline": session.get("baseline"),
            "want_baseline": session.get("want_baseline", True),
            "usage": session.get("usage", {}),
            "council_tokens": council_tokens,
            "token_budget": TOKEN_BUDGET,
            "sat_target": SAT_TARGET,
        }

# API Endpoints
@app.route("/")
def index():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    if os.path.exists(html_path):
        return send_file(html_path)
    return "<h3>Persona Council - index.html missing</h3>", 404

@app.route("/api/session", methods=["POST"])
def create_session():
    clean_expired_sessions()
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "solo").strip().lower()
    topic = data.get("topic", "").strip()
    want_baseline = bool(data.get("want_baseline", True))

    if not topic:
        return jsonify({"error": "Topic is required"}), 400

    if mode not in ["solo", "group"]:
        return jsonify({"error": "Mode must be 'solo' or 'group'"}), 400

    usage_init = {stage: {"calls": 0, "in": 0, "out": 0} for stage in STAGE_NAMES}

    if mode == "solo":
        raw_agents = data.get("agents", [])
        if not raw_agents or len(raw_agents) < 2:
            return jsonify({"error": "Solo council requires at least 2 personas"}), 400
        clean_agents = []
        seen = set()
        for a in raw_agents:
            name = a.get("name", "").strip()
            profile = a.get("profile", "").strip()
            if not name:
                return jsonify({"error": "Persona name cannot be empty"}), 400
            if name.lower() in seen:
                return jsonify({"error": f"Duplicate persona name: {name}"}), 400
            seen.add(name.lower())
            clean_agents.append({"name": name, "profile": profile or name})

        session_id = f"s_{int(time.time()*1000)}_{random.randint(100, 999)}"
        new_session = {
            "id": session_id,
            "mode": "solo",
            "topic": topic,
            "status": "running",
            "stage": "Initializing",
            "error": None,
            "agents": clean_agents,
            "claims": {},
            "check": None,
            "critiques": [],
            "rounds": [],
            "stop_reason": None,
            "baseline": None,
            "want_baseline": want_baseline,
            "usage": usage_init,
            "created_at": time.time(),
        }
        with session_lock:
            SESSIONS[session_id] = new_session

        threading.Thread(target=run_pipeline, args=(session_id,), daemon=True).start()
        return jsonify({"id": session_id, "mode": "solo", "status": "running"}), 201

    # Group mode: create lobby room
    room_code = generate_room_code()
    new_session = {
        "id": room_code,
        "mode": "group",
        "topic": topic,
        "status": "lobby",
        "stage": "Waiting for participants",
        "error": None,
        "agents": [],
        "claims": {},
        "check": None,
        "critiques": [],
        "rounds": [],
        "stop_reason": None,
        "baseline": None,
        "want_baseline": want_baseline,
        "usage": usage_init,
        "created_at": time.time(),
    }
    with session_lock:
        SESSIONS[room_code] = new_session

    return jsonify({"id": room_code, "mode": "group", "status": "lobby"}), 201

@app.route("/api/session/<sid>", methods=["GET"])
def get_session(sid):
    clean_expired_sessions()
    sid = sid.strip().upper() if len(sid) in [4, 5] and not sid.startswith("s_") else sid.strip()
    with session_lock:
        session = SESSIONS.get(sid)
        if not session:
            return jsonify({"error": f"Room '{sid}' not found"}), 404
        return jsonify(get_public_session(session)), 200

@app.route("/api/session/<sid>/join", methods=["POST"])
def join_session(sid):
    clean_expired_sessions()
    sid = sid.strip().upper() if len(sid) in [4, 5] and not sid.startswith("s_") else sid.strip()
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    must_haves = data.get("must_haves", "").strip()
    dealbreakers = data.get("dealbreakers", "").strip()
    constraints = data.get("constraints", "").strip()
    flexible_on = data.get("flexible_on", "").strip()

    if not name:
        return jsonify({"error": "Name is required"}), 400

    profile_parts = []
    if must_haves:
        profile_parts.append(f"Must-haves: {must_haves}")
    if dealbreakers:
        profile_parts.append(f"Dealbreakers: {dealbreakers}")
    if constraints:
        profile_parts.append(f"Constraints: {constraints}")
    if flexible_on:
        profile_parts.append(f"Flexible on: {flexible_on}")
    profile_str = "\n".join(profile_parts) if profile_parts else f"Participant {name}'s preferences."

    with session_lock:
        session = SESSIONS.get(sid)
        if not session:
            return jsonify({"error": f"Room '{sid}' not found"}), 404

        if session["status"] != "lobby":
            return jsonify({"error": "Cannot join: room session is already running or completed"}), 400

        if len(session["agents"]) >= 8:
            return jsonify({"error": "Room is full (max 8 participants)"}), 400

        for a in session["agents"]:
            if a["name"].lower() == name.lower():
                return jsonify({"error": f"The name '{name}' is already taken in this room"}), 400

        session["agents"].append({
            "name": name,
            "profile": profile_str
        })

        return jsonify({
            "success": True,
            "room": sid,
            "members": [a["name"] for a in session["agents"]]
        }), 200

@app.route("/api/session/<sid>/start", methods=["POST"])
def start_session(sid):
    clean_expired_sessions()
    sid = sid.strip().upper() if len(sid) in [4, 5] and not sid.startswith("s_") else sid.strip()
    with session_lock:
        session = SESSIONS.get(sid)
        if not session:
            return jsonify({"error": f"Room '{sid}' not found"}), 404

        if session["status"] != "lobby":
            return jsonify({"error": f"Session cannot be started from status '{session['status']}'"}), 400

        if len(session["agents"]) < 2:
            return jsonify({"error": "At least 2 participants must join before starting"}), 400

        session["status"] = "running"
        session["stage"] = "Initializing"

    threading.Thread(target=run_pipeline, args=(sid,), daemon=True).start()
    return jsonify({"success": True, "room": sid, "status": "running"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
