/* =========================================================================
 * The Teachings of the Masters — game engine
 * Vanilla JS single-page app. State persists in localStorage.
 * ========================================================================= */

const SAVE_KEY = "teachings-of-the-masters-v1";

/* ---------------- state ---------------- */

const defaultState = () => ({
  xp: 0,
  minutes: 0,
  sessions: 0,
  correctQuizzes: 0,
  streak: 0,
  lastPracticeDay: null,       // "YYYY-MM-DD"
  completed: {},               // { pathId: [stageIndex, ...] }
  achievements: [],            // [achievementId, ...]
});

let S = load();

function load() {
  try {
    const raw = localStorage.getItem(SAVE_KEY);
    if (raw) return { ...defaultState(), ...JSON.parse(raw) };
  } catch (e) { /* corrupted save — start fresh */ }
  return defaultState();
}

function save() {
  localStorage.setItem(SAVE_KEY, JSON.stringify(S));
}

/* ---------------- helpers ---------------- */

const $ = (sel) => document.querySelector(sel);
const app = () => $("#app");

function todayStr() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function rankFor(xp) {
  let r = AWARENESS_RANKS[0];
  for (const rk of AWARENESS_RANKS) if (xp >= rk.xp) r = rk;
  return r;
}

function nextRank(xp) {
  return AWARENESS_RANKS.find(r => r.xp > xp) || null;
}

function stagesDone(pathId) {
  return (S.completed[pathId] || []).length;
}

function isPathUnlocked(path) {
  if (!path.unlockedBy) return true;
  return stagesDone(path.unlockedBy.path) >= path.unlockedBy.stages;
}

function isStageUnlocked(path, idx) {
  if (idx === 0) return true;
  return (S.completed[path.id] || []).includes(idx - 1);
}

function isStageDone(path, idx) {
  return (S.completed[path.id] || []).includes(idx);
}

function fmtTime(sec) {
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/* ---------------- toasts & sound ---------------- */

function toast(html) {
  let holder = $("#toasts");
  if (!holder) {
    holder = document.createElement("div");
    holder.id = "toasts";
    document.body.appendChild(holder);
  }
  const t = document.createElement("div");
  t.className = "toast";
  t.innerHTML = html;
  holder.appendChild(t);
  setTimeout(() => t.remove(), 4200);
}

let audioCtx = null;
function bell(freq = 528, dur = 2.2) {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const t0 = audioCtx.currentTime;
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = "sine";
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, t0);
    gain.gain.exponentialRampToValueAtTime(0.35, t0 + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    osc.connect(gain).connect(audioCtx.destination);
    osc.start(t0);
    osc.stop(t0 + dur);
    // soft overtone
    const o2 = audioCtx.createOscillator();
    const g2 = audioCtx.createGain();
    o2.type = "sine";
    o2.frequency.value = freq * 2.7;
    g2.gain.setValueAtTime(0.0001, t0);
    g2.gain.exponentialRampToValueAtTime(0.08, t0 + 0.02);
    g2.gain.exponentialRampToValueAtTime(0.0001, t0 + dur * 0.6);
    o2.connect(g2).connect(audioCtx.destination);
    o2.start(t0);
    o2.stop(t0 + dur);
  } catch (e) { /* audio unavailable — stay silent */ }
}

/* ---------------- progression ---------------- */

function grantXP(amount, label) {
  const before = rankFor(S.xp).name;
  S.xp += amount;
  const after = rankFor(S.xp);
  toast(`✨ +${amount} Light — ${esc(label)}`);
  if (after.name !== before) {
    toast(`${after.icon} <b>Awareness rises!</b> You are now <b>${after.name}</b>`);
    bell(660);
  }
  save();
}

function touchStreak() {
  const today = todayStr();
  if (S.lastPracticeDay === today) return;
  const y = new Date(); y.setDate(y.getDate() - 1);
  const yesterday = `${y.getFullYear()}-${String(y.getMonth() + 1).padStart(2, "0")}-${String(y.getDate()).padStart(2, "0")}`;
  S.streak = (S.lastPracticeDay === yesterday) ? S.streak + 1 : 1;
  S.lastPracticeDay = today;
  save();
}

function unlockAch(id) {
  if (S.achievements.includes(id)) return;
  const a = ACHIEVEMENTS.find(x => x.id === id);
  if (!a) return;
  S.achievements.push(id);
  save();
  toast(`${a.icon} <b>Achievement:</b> ${esc(a.name)}`);
  bell(784, 1.6);
}

function checkAchievements() {
  if (S.sessions >= 1) unlockAch("first_sit");
  if (S.streak >= 3) unlockAch("streak_3");
  if (S.streak >= 7) unlockAch("streak_7");
  if (S.streak >= 30) unlockAch("streak_30");
  if (S.minutes >= 60) unlockAch("hour_total");
  if (S.minutes >= 300) unlockAch("five_hours");
  if (S.correctQuizzes >= 10) unlockAch("quiz_perfect");
  let all = true;
  for (const p of PATHS) {
    if (stagesDone(p.id) >= p.stages.length) unlockAch("path_" + p.id);
    else all = false;
  }
  if (all) unlockAch("all_paths");
}

/* ---------------- views ---------------- */

let activeTimers = [];
function clearTimers() {
  activeTimers.forEach(t => clearInterval(t));
  activeTimers = [];
}

function render(html) {
  clearTimers();
  app().innerHTML = html;
  window.scrollTo(0, 0);
}

function hudHTML() {
  const r = rankFor(S.xp);
  const nr = nextRank(S.xp);
  const pct = nr ? Math.min(100, Math.round(((S.xp - r.xp) / (nr.xp - r.xp)) * 100)) : 100;
  return `
  <header class="hud" onclick="viewAchievements()" title="View achievements">
    <div class="rank"><span class="icon">${r.icon}</span>${r.name}</div>
    <div class="stats">
      <div>✨ <b>${S.xp}</b> Light</div>
      <div>🔥 <b>${S.streak}</b> day streak</div>
      <div>⏳ <b>${S.minutes}</b> min</div>
      <div>🏆 <b>${S.achievements.length}</b>/${ACHIEVEMENTS.length}</div>
    </div>
    <div class="xpbar"><div style="width:${pct}%"></div></div>
  </header>`;
}

/* ----- home ----- */

function viewHome() {
  const cards = PATHS.map(p => {
    const unlocked = isPathUnlocked(p);
    const done = stagesDone(p.id);
    const total = p.stages.length;
    const pct = Math.round((done / total) * 100);
    let lockNote = "";
    if (!unlocked) {
      const req = PATHS.find(x => x.id === p.unlockedBy.path);
      lockNote = `<div class="small muted">🔒 Unlocks after ${p.unlockedBy.stages} stage${p.unlockedBy.stages > 1 ? "s" : ""} of <i>${esc(req.title)}</i></div>`;
    }
    return `
    <div class="card path-card ${unlocked ? "" : "locked"}" style="--path-color:${p.color}"
         ${unlocked ? `onclick="viewPath('${p.id}')"` : ""}>
      <div class="icon">${p.icon}</div>
      <div style="flex:1">
        <h2>${esc(p.title)}</h2>
        <div class="small src muted">${esc(p.source)}</div>
        ${lockNote}
        <div class="path-progress"><div style="width:${pct}%"></div></div>
        <div class="small muted" style="margin-top:.25rem">${done} / ${total} stages ${done === total ? "· complete 🪷" : ""}</div>
      </div>
    </div>`;
  }).join("");

  render(`
    ${hudHTML()}
    <h1>The Teachings of the Masters</h1>
    <p class="muted">The Wisdom of the Ages — a game of rising awareness. Six paths,
    one summit. Read a teaching, sit the practice, reflect, and watch your Light grow.</p>
    <div style="height:.8rem"></div>
    ${cards}
    <div class="card small muted">
      This app paraphrases ideas from six classic works for practice and study.
      It is a companion to the books, not a replacement — and not medical advice.
      Progress is saved on this device.
    </div>
  `);
}

/* ----- path ----- */

function viewPath(pathId) {
  const p = PATHS.find(x => x.id === pathId);
  const stages = p.stages.map((st, i) => {
    const done = isStageDone(p, i);
    const unlocked = isStageUnlocked(p, i);
    const mins = st.practice.minutes;
    const typeName = { timer: "silent sitting", breath: "breath practice", chakra: "chakra ascent", gaze: "awareness bells" }[st.practice.type];
    return `
    <div class="stage ${done ? "done" : ""} ${unlocked ? "" : "locked"}"
         ${unlocked ? `onclick="viewStage('${p.id}', ${i})"` : ""}>
      <div class="num">${done ? "✓" : i + 1}</div>
      <div style="flex:1">
        <b>${esc(st.title)}</b>
        <div class="meta">${mins} min · ${typeName}${done ? " · complete" : unlocked ? "" : " · 🔒"}</div>
      </div>
    </div>`;
  }).join("");

  render(`
    ${hudHTML()}
    <button class="backlink" onclick="viewHome()">← All paths</button>
    <div class="card" style="border-left:4px solid ${p.color}">
      <h1>${p.icon} ${esc(p.title)}</h1>
      <div class="small muted" style="font-style:italic">${esc(p.source)}</div>
      <p style="margin-top:.6rem">${esc(p.intro)}</p>
    </div>
    ${stages}
  `);
}

/* ----- stage (teaching) ----- */

function viewStage(pathId, idx) {
  const p = PATHS.find(x => x.id === pathId);
  const st = p.stages[idx];
  const done = isStageDone(p, idx);
  render(`
    ${hudHTML()}
    <button class="backlink" onclick="viewPath('${p.id}')">← ${esc(p.title)}</button>
    <div class="card" style="border-left:4px solid ${p.color}">
      <div class="small muted">Stage ${idx + 1} of ${p.stages.length} ${done ? "· complete ✓" : ""}</div>
      <h1>${esc(st.title)}</h1>
      <p class="teaching-text">${esc(st.teaching)}</p>
      <blockquote class="guidance">🧘 ${esc(st.practice.guidance)}</blockquote>
      <button class="btn gold" onclick="startSession('${p.id}', ${idx})">
        Begin practice — ${st.practice.minutes} min
      </button>
    </div>
  `);
}

/* ---------------- practice sessions ---------------- */

let session = null;

function startSession(pathId, idx) {
  const p = PATHS.find(x => x.id === pathId);
  const st = p.stages[idx];
  const pr = st.practice;
  session = {
    pathId, idx,
    total: pr.minutes * 60,
    left: pr.minutes * 60,
    taps: 0,
    running: true,
  };
  bell(432, 1.8);

  const body = {
    timer: timerBody, breath: breathBody, chakra: chakraBody, gaze: gazeBody,
  }[pr.type](pr, p);

  render(`
    ${hudHTML()}
    <div class="card session" style="border-left:4px solid ${p.color}">
      <div class="small muted">${esc(p.title)} — ${esc(st.title)}</div>
      <div class="clock" id="clock">${fmtTime(session.left)}</div>
      ${body}
      <div style="margin-top:1.2rem">
        <button class="btn ghost small" onclick="endSession(false)">End early</button>
      </div>
    </div>
  `);

  const tick = setInterval(() => {
    if (!session || !session.running) return;
    session.left--;
    const c = $("#clock");
    if (c) c.textContent = fmtTime(Math.max(0, session.left));
    stepPractice(pr);
    if (session.left <= 0) {
      clearInterval(tick);
      bell(432, 3);
      endSession(true);
    }
  }, 1000);
  activeTimers.push(tick);
}

/* --- practice bodies + per-second steppers --- */

function timerBody() {
  return `
    <div class="orb-wrap"><div class="orb big" id="orb"></div></div>
    <div class="phase-label" id="phase">Rest in stillness</div>`;
}

function breathBody(pr) {
  return `
    <div class="phase-label" id="phase">Breathe in…</div>
    <div class="orb-wrap"><div class="orb" id="orb"></div></div>
    <div class="small muted">Pattern: ${pr.pattern.filter(x => x > 0).join(" · ")}</div>`;
}

function chakraBody(pr) {
  const dots = CHAKRAS.slice(0, pr.upTo).map((c, i) =>
    `<div class="chakra-dot" id="ck${i}" style="--c:${c.color}" title="${c.name} — ${c.en}"></div>`
  ).join("");
  return `
    <div class="phase-label" id="phase">${CHAKRAS[0].name} — ${CHAKRAS[0].en}</div>
    <div class="chakra-col">${dots}</div>
    <div class="chakra-name" id="ckname">Attention at the ${CHAKRAS[0].note}</div>`;
}

function gazeBody() {
  return `
    <span class="gaze-flame">🕯️</span>
    <div class="phase-label" id="phase">Watch the mind</div>
    <button class="tap-bell" onclick="gazeTap()">🔔 I noticed my mind wandered</button>
    <div class="small muted" style="margin-top:.5rem" id="tapcount">0 moments of noticing</div>`;
}

function gazeTap() {
  if (!session) return;
  session.taps++;
  bell(880, 0.8);
  const el = $("#tapcount");
  if (el) el.textContent = `${session.taps} moment${session.taps === 1 ? "" : "s"} of noticing — each one is awareness returning`;
}

function stepPractice(pr) {
  const elapsed = session.total - session.left;
  if (pr.type === "breath") {
    const [inh, h1, exh, h2] = pr.pattern;
    const cycle = inh + h1 + exh + h2;
    const t = elapsed % cycle;
    const orb = $("#orb"), phase = $("#phase");
    if (!orb || !phase) return;
    if (t < inh) { orb.classList.add("big"); phase.textContent = "Breathe in…"; }
    else if (t < inh + h1) { phase.textContent = "Hold…"; }
    else if (t < inh + h1 + exh) { orb.classList.remove("big"); phase.textContent = "Breathe out…"; }
    else { phase.textContent = "Rest…"; }
  } else if (pr.type === "chakra") {
    const per = Math.floor(session.total / pr.upTo);
    const level = Math.min(pr.upTo - 1, Math.floor(elapsed / Math.max(1, per)));
    for (let i = 0; i <= level; i++) {
      const d = $("#ck" + i);
      if (d && !d.classList.contains("lit")) {
        d.classList.add("lit");
        if (i > 0) bell(300 + i * 60, 1.2);
      }
    }
    const c = CHAKRAS[level];
    const phase = $("#phase"), name = $("#ckname");
    if (phase) phase.textContent = `${c.name} — ${c.en}`;
    if (name) name.textContent = `Attention at the ${c.note}`;
  }
}

function endSession(finished) {
  if (!session) return;
  const { pathId, idx } = session;
  const p = PATHS.find(x => x.id === pathId);
  const st = p.stages[idx];
  const practicedMin = Math.max(1, Math.round((session.total - session.left) / 60));
  session = null;
  clearTimers();

  S.sessions++;
  S.minutes += practicedMin;
  touchStreak();

  if (finished) {
    grantXP(30 + st.practice.minutes * 8, "practice complete");
    checkAchievements();
    viewQuiz(pathId, idx);
  } else {
    grantXP(Math.max(5, practicedMin * 4), "partial practice");
    checkAchievements();
    toast("Every minute counts. The stage completes when the full practice is sat.");
    viewStage(pathId, idx);
  }
}

/* ---------------- quiz ---------------- */

function viewQuiz(pathId, idx) {
  const p = PATHS.find(x => x.id === pathId);
  const q = p.stages[idx].quiz;
  const opts = q.options.map((o, i) =>
    `<button class="quiz-opt" id="opt${i}" onclick="answerQuiz('${pathId}', ${idx}, ${i})">${esc(o)}</button>`
  ).join("");
  render(`
    ${hudHTML()}
    <div class="card" style="border-left:4px solid ${p.color}">
      <div class="small muted">Reflection — ${esc(p.stages[idx].title)}</div>
      <h2 style="margin:.4rem 0 .9rem">${esc(q.q)}</h2>
      ${opts}
      <div id="quizwhy" class="small muted" style="min-height:1.5rem;margin-top:.4rem"></div>
      <div id="quiznext" style="margin-top:.6rem"></div>
    </div>
  `);
}

function answerQuiz(pathId, idx, choice) {
  const p = PATHS.find(x => x.id === pathId);
  const q = p.stages[idx].quiz;
  const correct = choice === q.answer;
  q.options.forEach((_, i) => {
    const b = $("#opt" + i);
    b.disabled = true;
    if (i === q.answer) b.classList.add("correct");
    else if (i === choice) b.classList.add("wrong");
  });
  $("#quizwhy").textContent = q.why;
  if (correct) {
    S.correctQuizzes++;
    grantXP(20, "clear reflection");
  } else {
    grantXP(5, "honest attempt");
  }
  completeStage(pathId, idx);
  const done = stagesDone(pathId);
  const total = p.stages.length;
  const nextLabel = idx + 1 < total ? "Next stage →" : "Back to path 🪷";
  $("#quiznext").innerHTML =
    `<button class="btn gold" onclick="${idx + 1 < total ? `viewStage('${pathId}', ${idx + 1})` : `viewPath('${pathId}')`}">${nextLabel}</button>`;
  if (done === total) {
    toast(`${p.icon} <b>Path complete:</b> ${esc(p.title)}`);
  }
}

function completeStage(pathId, idx) {
  S.completed[pathId] = S.completed[pathId] || [];
  if (!S.completed[pathId].includes(idx)) {
    S.completed[pathId].push(idx);
    grantXP(40, "stage mastered");
  }
  save();
  checkAchievements();
}

/* ---------------- achievements ---------------- */

function viewAchievements() {
  const grid = ACHIEVEMENTS.map(a => {
    const got = S.achievements.includes(a.id);
    return `
    <div class="ach ${got ? "" : "locked"}">
      <div class="icon">${a.icon}</div>
      <div><b>${esc(a.name)}</b><span>${esc(a.desc)}</span></div>
    </div>`;
  }).join("");
  const r = rankFor(S.xp);
  const ranks = AWARENESS_RANKS.map(rk =>
    `<div class="small" style="opacity:${S.xp >= rk.xp ? 1 : .4}">${rk.icon} <b>${rk.name}</b> — ${rk.xp} Light${rk.name === r.name ? " ← you are here" : ""}</div>`
  ).join("");
  render(`
    ${hudHTML()}
    <button class="backlink" onclick="viewHome()">← All paths</button>
    <h1>🏆 Achievements</h1>
    <div class="ach-grid">${grid}</div>
    <div style="height:1rem"></div>
    <div class="card">
      <h2>Ranks of Awareness</h2>
      ${ranks}
    </div>
    <div class="card">
      <h2>Journey so far</h2>
      <div class="small">Sessions: <b>${S.sessions}</b> · Minutes: <b>${S.minutes}</b> · Reflections answered well: <b>${S.correctQuizzes}</b></div>
      <div style="margin-top:.7rem">
        <button class="btn ghost small" onclick="resetProgress()">Reset all progress</button>
      </div>
    </div>
  `);
}

function resetProgress() {
  if (!confirm("Begin again as a Seeker? All progress on this device will be erased.")) return;
  S = defaultState();
  save();
  viewHome();
}

/* ---------------- boot ---------------- */

window.addEventListener("beforeunload", (e) => {
  if (session && session.running) {
    e.preventDefault();
    e.returnValue = "";
  }
});

viewHome();
