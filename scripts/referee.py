"""Referee bot for the Research Gap Heist.

Runs on every new or edited issue, and once more when the instructor clicks
"Reveal". It rebuilds the whole game state from all issues each time, so it
never gets out of sync.

Scores stay hidden until the reveal, so teams cannot learn which cards are
fake by watching the leaderboard.
"""
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = json.load(open(os.path.join(ROOT, "game.json")))
CARD_IDS = list(CONFIG["cards"].keys())
REVEALED_FLAG = os.path.join(ROOT, ".game", "revealed")
START_FILE = os.path.join(ROOT, ".game", "start")

# ---------------------------------------------------------------- parsing

TYPES = {"[JOIN]": "join", "[VERDICT]": "verdict", "[MAP]": "map", "[GAP]": "gap", "[REVIEW]": "review"}
FIELD_SIGNATURE = {
    "join": {"Team", "Your name"},
    "verdict": {"Card", "Verdict", "Proof"},
    "map": {"Themes"},
    "gap": {"Gap type", "Paragraph", "Research question"},
    "review": {"Gap issue number", "Checklist"},
}


def parse_form(body):
    fields = {}
    for chunk in re.split(r"^### ", body or "", flags=re.M)[1:]:
        label, _, value = chunk.partition("\n")
        value = value.strip()
        if value == "_No response_":
            value = ""
        fields[label.strip()] = value
    return fields


def issue_type(issue, fields):
    title = (issue.get("title") or "").strip().upper()
    for prefix, kind in TYPES.items():
        if title.startswith(prefix):
            return kind
    for kind, sig in FIELD_SIGNATURE.items():
        if sig.issubset(fields.keys()):
            return kind
    return None


def card_ids(text):
    seen = []
    for m in re.findall(r"\bC\s?(\d{1,2})\b", text or "", flags=re.I):
        cid = "C%02d" % int(m)
        if cid in CONFIG["cards"] and cid not in seen:
            seen.append(cid)
    return seen


def verdict_word(v):
    v = (v or "").strip().lower()
    for w in ("real", "twisted", "fake"):
        if v.startswith(w):
            return w.capitalize()
    return ""


def proof_ok(p):
    p = (p or "").lower()
    return "http" in p or "doi.org" in p or "not found" in p


# ---------------------------------------------------------------- game state

def build_state(issues):
    """Turn the list of issues into teams and submissions. No answer key needed."""
    start = open(START_FILE).read().strip() if os.path.exists(START_FILE) else ""
    issues = sorted([i for i in issues if "pull_request" not in i and i.get("created_at", "") >= start],
                    key=lambda i: i["number"])
    members = {}                      # login -> team
    roster = defaultdict(list)        # team -> [(login, name)]
    verdicts = defaultdict(dict)      # team -> card -> submission
    maps, gaps = {}, {}               # team -> latest submission
    reviews = []
    notes = {}                        # issue number -> list of messages for the author
    gap_issue_team = {}

    for iss in issues:
        f = parse_form(iss.get("body"))
        kind = issue_type(iss, f)
        user = iss["user"]["login"]
        n = iss["number"]
        msgs = notes.setdefault(n, [])
        if kind is None:
            msgs.append("I could not read this issue. Please use one of the forms in **New issue**.")
            continue

        if kind == "join":
            team = f.get("Team", "")
            if team not in CONFIG["teams"]:
                msgs.append("Please pick a team from the list.")
            elif user in members:
                msgs.append(f"You are already on **{members[user]}**. Each person can join only one team.")
            elif len(roster[team]) >= CONFIG["max_team_size"]:
                msgs.append(f"**{team}** is full. Please join another team.")
            else:
                members[user] = team
                roster[team].append((user, f.get("Your name", "")))
                msgs.append(f"Welcome to **{team}**, {f.get('Your name', user)}! Next, open CARDS.md and start checking cards (Step 1).")
            continue

        team = members.get(user)
        if not team:
            msgs.append("You are not on a team yet. Please do **Step 0: Join a team** first, then submit this again.")
            continue

        if kind == "verdict":
            ids = card_ids(f.get("Card", ""))
            v = verdict_word(f.get("Verdict"))
            if not ids or not v:
                msgs.append("Please pick a card and a verdict.")
                continue
            cid = ids[0]
            if cid in verdicts[team]:
                msgs.append(f"Your team already locked in a verdict for **{cid}** (issue #{verdicts[team][cid]['issue']}). Only the first one counts.")
                continue
            sub = {"issue": n, "verdict": v, "proof_ok": proof_ok(f.get("Proof")), "by": user}
            verdicts[team][cid] = sub
            done = len(verdicts[team])
            msgs.append(f"Locked in: **{cid} = {v}** for {team}. Your team has checked {done} of {len(CARD_IDS)} cards.")
            if not sub["proof_ok"]:
                msgs.append("Warning: your proof has no link and does not say NOT FOUND. A correct verdict without proof gets only half points.")
            if v == "Twisted" and not f.get("If Twisted, what does the paper really say?"):
                msgs.append("Tip: for a Twisted card, say what the paper really found.")
            if done == len(CARD_IDS):
                msgs.append("All cards checked. Move on to **Step 2: Submit your literature map**.")

        elif kind == "map":
            themes = []
            for line in f.get("Themes", "").splitlines():
                if ":" in line:
                    name, _, rest = line.partition(":")
                    ids = card_ids(rest)
                    if name.strip() and ids:
                        themes.append((name.strip(), ids))
            pair = card_ids(f.get("Two cards that disagree", ""))[:2]
            maps[team] = {"issue": n, "themes": themes, "pair": pair}
            if not themes:
                msgs.append("I could not find any themes. Use one line per theme, like `Bias in the data: C01, C02`.")
            else:
                msgs.append(f"Map saved for **{team}** ({len(themes)} theme{'s' if len(themes) != 1 else ''}). If you submit again, the newest map replaces this one.")
                msgs.append(mermaid(themes))
                if len(themes) < 2:
                    msgs.append("Tip: a good map has at least 2 themes.")
            msgs.append("Next: **Step 3: Crack the gap**.")

        elif kind == "gap":
            para = f.get("Paragraph", "")
            words = len(re.findall(r"\b\w+\b", para))
            has_therefore = "therefore, this study aims to" in para.lower()
            traced = card_ids(f.get("Trace", ""))
            gaps[team] = {"issue": n, "type": f.get("Gap type", ""), "words": words,
                          "therefore": has_therefore, "traced": traced,
                          "ailog": bool(f.get("AI use log", "").strip())}
            gap_issue_team[n] = team
            msgs.append(f"Gap saved for **{team}**. Quick check:")
            msgs.append(f"- Paragraph length: {words} words {'(good)' if words >= 80 else '(needs at least 80)'}")
            msgs.append(f"- Ends with \"Therefore, this study aims to...\": {'yes' if has_therefore else 'not found'}")
            msgs.append(f"- Cards in your trace: {', '.join(traced) if traced else 'none found'}")
            msgs.append("You can submit again to replace this. Next: **Step 4: Review a rival team**. Give them this issue number: #%d" % n)

        elif kind == "review":
            target = re.findall(r"\d+", f.get("Gap issue number", ""))
            ticks = len(re.findall(r"- \[[xX]\]", f.get("Checklist", "")))
            fix_words = len(re.findall(r"\b\w+\b", f.get("One thing to fix", "")))
            reviews.append({"issue": n, "team": team, "target": int(target[0]) if target else None,
                            "ticks": ticks, "fix_words": fix_words})
            msgs.append(f"Review received from **{team}**.")
            if fix_words < 15:
                msgs.append("Your \"one thing to fix\" is too short (need at least 15 words), so this review will not earn points. Submit a new one.")

    # check review targets now that all gap issues are known
    valid_reviews = []
    for r in reviews:
        tgt_team = gap_issue_team.get(r["target"])
        if r["target"] is None or tgt_team is None:
            notes[r["issue"]].append("That issue number is not a [GAP] issue. Check the number and submit again.")
        elif tgt_team == r["team"]:
            notes[r["issue"]].append("You cannot review your own team. Pick a rival team.")
        elif r["fix_words"] >= 15:
            r["target_team"] = tgt_team
            valid_reviews.append(r)
            notes[r["issue"]].append(f"This review counts. **{tgt_team}** has been reviewed.")

    return {"members": members, "roster": roster, "verdicts": verdicts, "maps": maps,
            "gaps": gaps, "reviews": valid_reviews, "notes": notes}


def mermaid(themes):
    lines = ["```mermaid", "graph TD", f'  T["{esc(CONFIG["topic"])}"]']
    for i, (name, ids) in enumerate(themes):
        lines.append(f'  T --> TH{i}["{esc(name)}"]')
        for cid in ids:
            lines.append(f'  TH{i} --> {cid}_{i}["{cid}: {esc(CONFIG["cards"][cid])}"]')
    lines.append("```")
    return "\n".join(lines)


def esc(s):
    return s.replace('"', "'").replace("[", "(").replace("]", ")")


# ---------------------------------------------------------------- scoring (needs the answer key)

def score(state, key):
    answers = key["verdicts"]
    pair_set = [set(p) for p in key.get("contradiction_pairs", [])]
    good_types = set(key.get("good_gap_types", []))
    out = {}
    for team in CONFIG["teams"]:
        if team not in state["roster"]:
            continue
        s = {"verify": 0.0, "map": 0.0, "gap": 0.0, "review": 0.0, "cards": {}}

        for cid, sub in state["verdicts"][team].items():
            truth = answers[cid]
            if sub["verdict"] == truth:
                pts = 2 if truth == "Real" else 3
                if not sub["proof_ok"]:
                    pts /= 2
                s["cards"][cid] = "right"
            else:
                pts = -1 if (truth == "Real" and sub["verdict"] == "Fake") else 0
                s["cards"][cid] = "wrong"
            s["verify"] += pts

        m = state["maps"].get(team)
        if m:
            placed = {cid for _, ids in m["themes"] for cid in ids}
            for cid in placed:
                s["map"] += {"Real": 1, "Twisted": -1, "Fake": -2}[answers[cid]]
            if len(m["themes"]) >= 2:
                s["map"] += 1
            if len(m["pair"]) == 2:
                s["map"] += 3 if set(m["pair"]) in pair_set else -1

        g = state["gaps"].get(team)
        if g:
            s["gap"] += 2 if g["therefore"] else 0
            s["gap"] += 1 if g["words"] >= 80 else 0
            s["gap"] += 2 if g["type"] in good_types else 0
            s["gap"] += 1 if g["ailog"] else 0
            real_traced = [c for c in g["traced"] if answers[c] == "Real"]
            s["gap"] += min(3, len(real_traced))
            for c in g["traced"]:
                s["gap"] += {"Real": 0, "Twisted": -1, "Fake": -2}[answers[c]]

        given = {}
        for r in state["reviews"]:
            if r["team"] == team:
                given[r["target_team"]] = r
        s["review"] += 2 * min(2, len(given))
        received = {}
        for r in state["reviews"]:
            if r.get("target_team") == team:
                received[r["team"]] = r["ticks"]
        if received:
            s["review"] += round(sum(received.values()) / len(received), 1)

        s["total"] = round(s["verify"] + s["map"] + s["gap"] + s["review"], 1)
        out[team] = s
    return out


# ---------------------------------------------------------------- output files

def leaderboard_live(state):
    rows = ["# Leaderboard", "",
            "Points are **hidden** until your instructor reveals the answers. Keep going!", "",
            "| Team | Members | Cards checked | Map | Gap | Reviews given |",
            "|---|---|---|---|---|---|"]
    given = defaultdict(set)
    for r in state["reviews"]:
        given[r["team"]].add(r["target_team"])
    for team in CONFIG["teams"]:
        if team not in state["roster"]:
            continue
        rows.append("| %s | %d | %d / %d | %s | %s | %d |" % (
            team, len(state["roster"][team]), len(state["verdicts"][team]), len(CARD_IDS),
            "done" if team in state["maps"] else "-", "done" if team in state["gaps"] else "-",
            len(given[team])))
    if len(rows) == 6:
        rows.append("| No teams yet | | | | | |")
    return "\n".join(rows) + "\n"


def leaderboard_final(state, scores):
    ranked = sorted(scores.items(), key=lambda kv: -kv[1]["total"])
    rows = ["# Final Leaderboard", "", "The answers are out. See ANSWERS.md for every card.", "",
            "| Rank | Team | Check cards | Map | Gap | Review | Total |",
            "|---|---|---|---|---|---|---|"]
    for i, (team, s) in enumerate(ranked, 1):
        rows.append("| %d | %s | %g | %g | %g | %g | **%g** |" % (i, team, s["verify"], s["map"], s["gap"], s["review"], s["total"]))
    rows += ["", "## Card by card", "", "Y = right, N = wrong, blank = not checked", "",
             "| Team | " + " | ".join(CARD_IDS) + " |", "|---|" + "---|" * len(CARD_IDS)]
    for team, s in ranked:
        cells = [{"right": "Y", "wrong": "N"}.get(s["cards"].get(c), "") for c in CARD_IDS]
        rows.append(f"| {team} | " + " | ".join(cells) + " |")
    return "\n".join(rows) + "\n"


def answers_md(key):
    rows = ["# Answers", ""]
    for cid in CARD_IDS:
        rows.append(f"**{cid}: {CONFIG['cards'][cid]}** is **{key['verdicts'][cid]}**. {key['explanations'][cid]}")
        rows.append("")
    rows += ["## The disagreement", "", key.get("pair_explanation", ""), "",
             "## Good gap types for this set", "", key.get("gap_explanation", ""), ""]
    return "\n".join(rows)


# ---------------------------------------------------------------- GitHub API

def api(method, path, data=None):
    url = "https://api.github.com" + path
    req = urllib.request.Request(url, method=method, data=json.dumps(data).encode() if data is not None else None)
    req.add_header("Authorization", "Bearer " + os.environ["GITHUB_TOKEN"])
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req) as r:
        body = r.read()
        return json.loads(body) if body else None


def all_issues(repo):
    out, page = [], 1
    while True:
        batch = api("GET", f"/repos/{repo}/issues?state=all&per_page=100&page={page}")
        out += batch
        if len(batch) < 100:
            return out
        page += 1


def write(path, text):
    full = os.path.join(ROOT, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as fh:
        fh.write(text)


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    reveal = "--reveal" in sys.argv
    key_raw = os.environ.get("ANSWER_KEY", "").strip()

    if os.path.exists(REVEALED_FLAG) and not reveal:
        event = json.load(open(os.environ["GITHUB_EVENT_PATH"]))
        if event.get("action") == "opened":
            n = event["issue"]["number"]
            api("POST", f"/repos/{repo}/issues/{n}/comments", {"body": "The game is over. Check LEADERBOARD.md and ANSWERS.md."})
        return

    state = build_state(all_issues(repo))

    if reveal:
        if not key_raw:
            sys.exit("ANSWER_KEY secret is missing. Add it in Settings > Secrets and variables > Actions.")
        key = json.loads(key_raw)
        scores = score(state, key)
        write("LEADERBOARD.md", leaderboard_final(state, scores))
        write("ANSWERS.md", answers_md(key))
        write(".game/revealed", "revealed\n")
        return

    write("LEADERBOARD.md", leaderboard_live(state))

    event = json.load(open(os.environ["GITHUB_EVENT_PATH"]))
    if event.get("action") != "opened":
        return
    iss = event["issue"]
    n = iss["number"]
    msgs = state["notes"].get(n) or ["Got it."]
    api("POST", f"/repos/{repo}/issues/{n}/comments", {"body": "\n\n".join(msgs)})
    kind = issue_type(iss, parse_form(iss.get("body")))
    if kind in ("join", "verdict", "review", None):
        api("PATCH", f"/repos/{repo}/issues/{n}", {"state": "closed"})


if __name__ == "__main__":
    main()
