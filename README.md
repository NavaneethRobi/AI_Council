# Persona Council 🏛️
> **Private Personal AIs Negotiating Group Consensus**

Built for an 8-hour hackathon. Persona Council gives every participant their own autonomous AI representative. The personal AIs negotiate a group decision, rate the outcome privately for their own human stakeholder, and iterate through structured revisions until everyone is satisfied or the token budget is reached. It also directly benchmarks the multi-agent consensus against a single plain LLM call with transparent token accounting.

---

## 🚀 Quick Start

### 1. Installation
Ensure you have Python 3.9+ installed.

```bash
pip install -r requirements.txt
```

### 2. Environment Variables
Persona Council is powered by **Gemma 4** through the Gemini API (provided via MLH), with optional support for OpenAI-compatible endpoints.

```bash
# Required: API Key
export LLM_API_KEY="your-gemini-api-key"

# Optional Model Configuration (defaults shown):
export LLM_MODEL="gemma-4-26b-a4b-it"          # Fast model for proposals, checks, critiques, and ratings
export LLM_MODEL_STRONG="gemma-4-31b-it"        # Strong model for verdicts, revisions, and baseline
export THINKING_LEVEL="minimal"                 # Gemma thinking budget: "minimal", "low", etc. (or "" to omit)

# Consensus & Budget Controls:
export SAT_TARGET="4"                          # Satisfaction threshold (1-5 scale, default: 4)
export MAX_REVISIONS="2"                        # Max revision iterations (default: 2)
export TOKEN_BUDGET="25000"                     # Hard council token ceiling (default: 25,000)

# Optional OpenAI-Compatible Provider:
# export LLM_PROVIDER="openai"
# export LLM_BASE_URL="https://api.together.xyz/v1"
```

*On Windows PowerShell, use `$env:LLM_API_KEY="your-gemini-api-key"`.*

### 3. Running the Server
```bash
python app.py
```

The application starts on `0.0.0.0:5000`:
- **Desktop/Local**: Open `http://localhost:5000`
- **Mobile/Phones on Local Network**: Find your laptop's local IP (e.g. `192.168.1.45`) and open `http://<laptop-ip>:5000` on any phone browser.

---

## 💡 How It Works & Two Modes

### 1. Solo Council
- A single person exploring a high-stakes trade-off.
- Set up custom or default council personas (e.g., **Optimist**, **Skeptic**, **Pragmatist**).
- Starts immediately upon submission.

### 2. Group Room
- Host creates a room and receives a 4-character code (e.g., `K9QM`) and a share link: `http://<host>:5000/?room=K9QM`.
- Up to 8 participants join from their own devices.
- Each person enters their name and four private fields:
  1. **Must-haves** (Non-negotiables)
  2. **Dealbreakers** (Unacceptable outcomes)
  3. **Constraints** (Time, budget, physical restrictions)
  4. **Flexible on** (Areas open to compromise)
- Anyone can click **Start Council** once at least 2 people join.
- All participants watch the negotiation progress live via 1.5s non-destructive polling.

---

## 🔒 Strict Privacy Guarantee

In Group Mode:
1. **Public API Sanitization**: The public state (`GET /api/session/<id>`) **NEVER** includes any participant's raw profile or dealbreakers. Only participant names and compact synthesized claims are broadcast.
2. **Agent-to-Agent Isolation**: An agent only shares its compact position, points, and risks with the room. Other agents never see another participant's raw notes.
3. **Compromise Defense**: Agents only concede points where their human indicated they were "Flexible on", while fiercely defending "Must-haves" and rejecting "Dealbreakers".

---

## ⚡ The 7-Stage Pipeline & Cost Efficiency

Standard multi-agent debate burns hundreds of thousands of tokens by having agents exchange long unconstrained essays. Persona Council achieves consensus at a fraction of the cost through 5 key architectural decisions:

1. **Dual Model Tiering**: Fast model (`gemma-4-26b-a4b-it`) runs high-volume parallel stages; strong model (`gemma-4-31b-it`) is reserved for the moderator verdict, revisions, and baseline.
2. **Compact JSON Claims**: Proposals are strictly capped at $\le 20$ words for stance, $\le 3$ points ($\le 15$ words), and $\le 2$ risks ($\le 15$ words) with `max_tokens: 250`.
3. **Early Disagreement Filtering**: Stage 2 evaluates compatibility in a single $60$-token call. If positions align, the Critique stage is skipped entirely.
4. **Conditional Compromise Critique**: If tension exists, agents produce at most 3 concise compromise suggestions ($\le 20$ words each).
5. **Output Caps & Revision Guardrails**: Revisions are bounded by `SAT_TARGET`, `MAX_REVISIONS`, and `TOKEN_BUDGET`.

### Pipeline Summary:
1. **1. Proposals** (Parallel, Fast model, JSON schema, max 250 tok)
2. **2. Disagreement check** (Fast model, `AGREE` vs `DISAGREE: <issue>`, max 60 tok)
3. **3. Critique** (Conditional on disagreement, Parallel, Fast model, max 250 tok)
4. **4. Verdict** (Strong model, `DECISION / WHY / TRADE-OFFS / FIRST STEP`, max 350 tok)
5. **5. Satisfaction check** (Parallel, Fast model, honest 1–5 score + 20-word reason, max 80 tok)
6. **6. Revision loop** (Runs if any score $< 4$; stops on consensus, max revisions, or token cap)
7. **Baseline Benchmark** (Single direct strong-model call + fast ratings for objective comparison)

---

## 📊 Council vs. Single-Call Baseline

Does multi-agent consensus actually produce better outcomes than just asking a single strong LLM?
Persona Council answers this with real data:
- Runs an optional parallel single-call baseline on the strong model given all constraints.
- Has each participant's personal AI rate both outcomes using identical criteria.
- Displays a head-to-head card comparing **Lowest Score**, **Average Score**, and **Token Delta**.
- Displays an exact breakdown of input, output, and thought tokens per stage.

---

## ⚠️ Honest Limits & Nuances

- **Privacy Scope**: Data is strictly private between participants on the network. However, private notes are processed by the server and sent to the LLM API provider in their respective agent prompts.
- **Same-Model Scoring**: Satisfaction scores come from the fast Gemma model. While prompt instructions mandate critical, honest evaluation rather than politeness, scores are comparative and indicative rather than absolute mathematical truth.
- **In-Memory Volatility**: To maintain zero-dependency 8-hour hackathon simplicity, sessions and rooms are stored in-memory (with a 6-hour TTL). Restarting `app.py` wipes active rooms.
- **Network Discovery**: To join via phone, both host and phones must be on the same Wi-Fi network with port 5000 accessible.

---

## 📁 Repository Structure
```
├── app.py             # Flask server, session store, Gemma 4 LLM client, consensus engine
├── index.html         # Responsive dark-theme SPA (Vanilla JS + CSS, zero build step)
├── requirements.txt   # flask, requests
├── README.md          # Documentation & operational guide
└── hackathon.md       # Hackathon mode time-budget & phase gate rules
```
