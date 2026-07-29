/* =========================================================================
 * The Teachings of the Masters — game content
 *
 * All teaching text below is an original paraphrase / summary of ideas from
 * the source traditions, written for this app. It is not reproduced text.
 * ========================================================================= */

const AWARENESS_RANKS = [
  { name: "Seeker",      xp: 0,     icon: "🌱" },
  { name: "Student",     xp: 300,   icon: "📿" },
  { name: "Initiate",    xp: 900,   icon: "🕯️" },
  { name: "Adept",       xp: 2000,  icon: "🔮" },
  { name: "Mystic",      xp: 4000,  icon: "🌙" },
  { name: "Master",      xp: 7000,  icon: "☀️" },
  { name: "Illumined",   xp: 11000, icon: "✨" },
  { name: "Enlightened", xp: 16000, icon: "🪷" },
];

/* Practice types:
 *  - "timer"   : silent meditation with bell, optional mantra hint
 *  - "breath"  : guided breath pacer (pattern = [inhale, hold, exhale, hold] seconds)
 *  - "chakra"  : chakra ascension — awareness rises through centers over the session
 *  - "gaze"    : wandering-mind bell practice (tap when you notice the mind wandered)
 */

const PATHS = [
  /* ------------------------------------------------------------------ */
  {
    id: "masters",
    title: "The Way of the Masters",
    source: "The Book of the Masters · R. Swinburne Clymer",
    color: "#c9a227",
    icon: "🗝️",
    intro:
      "The Masters teach that divinity is not given from outside but awakened " +
      "within. The soul is a spark of the divine fire; the work of life is to " +
      "fan that spark into flame through conscious effort, purity of motive, " +
      "and daily practice. This is the foundation path — walk it first.",
    unlockedBy: null,
    stages: [
      {
        title: "The Spark Within",
        teaching:
          "Every human being carries a divine spark — a seed of soul that is " +
          "real but dormant. Nothing outside you can awaken it; no teacher can " +
          "hand you enlightenment. The Master only points the way. Your first " +
          "practice is simply to sit with the conviction that something in you " +
          "is already luminous, and to give it your quiet attention.",
        practice: { type: "timer", minutes: 3,
          guidance: "Sit upright. Rest attention in the center of the chest and silently affirm: “There is a light in me.” When the mind drifts, gently return." },
        quiz: {
          q: "According to the Masters, who can awaken the divine spark within you?",
          options: ["A sufficiently advanced teacher", "Only you, through your own effort", "It awakens on its own at death", "Ritual alone"],
          answer: 1,
          why: "Teachers point the way, but soul growth is won only by one's own conscious effort."
        }
      },
      {
        title: "Thought Builds the Soul",
        teaching:
          "The Masters hold that thought is a creative force. Every thought " +
          "either feeds the soul-flame or smothers it. Anger, envy, and fear " +
          "are not merely feelings — they are materials you build yourself " +
          "from. The aspirant therefore begins to watch thought as a gardener " +
          "watches soil: not with violence, but with steady, honest attention.",
        practice: { type: "gaze", minutes: 4,
          guidance: "Watch the stream of thought. Each time you notice you were carried away, ring the inner bell (tap) and return. Noticing IS the practice — every tap is a victory, not a failure." },
        quiz: {
          q: "In this teaching, noticing that your mind wandered is…",
          options: ["A failure of discipline", "The practice itself — a moment of awakened awareness", "A sign you should stop meditating", "Irrelevant"],
          answer: 1,
          why: "Each moment of noticing is a moment the light of awareness returns. That is the exercise."
        }
      },
      {
        title: "Purity of Motive",
        teaching:
          "Power without purity destroys the one who wields it. The Masters " +
          "warn that occult development sought for pride, gain, or power over " +
          "others closes the very door it tries to force. The soul unfolds " +
          "only in an atmosphere of harmlessness and service. Ask before every " +
          "practice: for whom do I do this work? Let the answer include more " +
          "than yourself.",
        practice: { type: "timer", minutes: 5,
          guidance: "Begin by silently dedicating this sitting to the well-being of someone you love, then to someone neutral, then to all beings. Rest in that widened heart." },
        quiz: {
          q: "Why do the Masters insist on purity of motive before power?",
          options: ["Power sought for self closes the door to real unfoldment", "Because rules are rules", "Purity makes practice easier to schedule", "It doesn't matter"],
          answer: 0,
          why: "Development driven by pride or gain corrupts and arrests itself; harmlessness and service keep the way open."
        }
      },
      {
        title: "The Daily Fire",
        teaching:
          "Transmutation — turning the lower nature into the higher — is not a " +
          "single dramatic event but a daily fire, tended in small acts: the " +
          "impulse restrained, the kindness chosen, the practice kept even " +
          "when dull. The Masters call regularity the first magic. A little, " +
          "done every day, outweighs heroics done once.",
        practice: { type: "breath", minutes: 5, pattern: [4, 4, 4, 4],
          guidance: "Square breathing: inhale 4, hold 4, exhale 4, hold 4. Let the evenness of the breath teach the evenness of daily practice." },
        quiz: {
          q: "What do the Masters call “the first magic”?",
          options: ["Secret words of power", "Regularity — small practice kept daily", "Fasting", "Reading many books"],
          answer: 1,
          why: "Steady daily tending of the inner fire accomplishes what occasional heroic effort cannot."
        }
      },
      {
        title: "Initiation Is Within",
        teaching:
          "The outer ceremonies of every tradition are only pictures of an " +
          "inner event: the moment the soul takes command of the personality. " +
          "You are initiated not when a ritual is performed over you, but when " +
          "awareness, once glimpsed, becomes the ruling principle of your " +
          "life. From this stage forward, every path in this journey is a " +
          "different window on that same event.",
        practice: { type: "timer", minutes: 8,
          guidance: "Sit in silence. Whatever arises — sound, sensation, thought — meet it as the sovereign soul meets a visitor: aware, unmoved, kind." },
        quiz: {
          q: "When, according to this teaching, does true initiation occur?",
          options: ["When a ritual is performed over you", "When you join an order", "When awareness becomes the ruling principle of your life", "At physical death"],
          answer: 2,
          why: "Ceremony only pictures the inner event — the soul taking command of the personality."
        }
      },
    ],
  },

  /* ------------------------------------------------------------------ */
  {
    id: "zohar",
    title: "The Tree of Splendor",
    source: "The Zohar — The Book of Splendor",
    color: "#7c5cff",
    icon: "🌳",
    intro:
      "The Zohar reads the world as a garment woven of light. Behind every " +
      "form shines a hidden radiance, and the soul's task is to raise the " +
      "sparks — to find the concealed light within the ordinary. Climb the " +
      "Tree of Life from the earth of everyday awareness toward the crown.",
    unlockedBy: { path: "masters", stages: 2 },
    stages: [
      {
        title: "The Hidden Light",
        teaching:
          "The Zohar teaches that the first light of creation was too vast for " +
          "the world and so was hidden — concealed inside creation itself, " +
          "waiting to be found by those who seek. Meditation, in this view, is " +
          "the art of noticing the radiance hidden inside plain moments: " +
          "breath, bread, the face of another person.",
        practice: { type: "timer", minutes: 4,
          guidance: "Choose one plain thing — the breath. Attend to it as though it were concealing a great light. Curiosity, not effort." },
        quiz: {
          q: "In the Zohar's telling, where was the primordial light hidden?",
          options: ["Destroyed forever", "Within creation itself, for seekers to find", "On the moon", "In the Temple only"],
          answer: 1,
          why: "The light was concealed inside the world — which is why contemplation can uncover it anywhere."
        }
      },
      {
        title: "Malkhut — The Kingdom",
        teaching:
          "The lowest sphere of the Tree, Malkhut, is this world: body, " +
          "matter, the here-and-now. The mystics insist the climb begins here " +
          "— not by escaping the body but by inhabiting it fully. A distracted " +
          "person is not standing in the Kingdom; a present one is.",
        practice: { type: "timer", minutes: 5,
          guidance: "Body-scan: move attention slowly from the crown of the head to the soles of the feet. Arrive completely in the Kingdom of the body." },
        quiz: {
          q: "How does the climb up the Tree of Life begin?",
          options: ["By leaving the body behind", "By full presence in the body and the world (Malkhut)", "By memorizing the names of the spheres", "By fasting"],
          answer: 1,
          why: "Malkhut, the material world, is the root of the ladder — presence here is the first rung."
        }
      },
      {
        title: "The Six Days of the Heart",
        teaching:
          "Between earth and crown stand the emanations of the heart — " +
          "kindness, strength, beauty, endurance, splendor, foundation. The " +
          "Zohar sees these not as abstractions but as living currents that " +
          "flow through a person when the channels are clear. Kindness " +
          "unbalanced becomes indulgence; strength unbalanced becomes cruelty; " +
          "beauty is their harmony.",
        practice: { type: "breath", minutes: 6, pattern: [5, 0, 5, 0],
          guidance: "Even breathing, 5 in / 5 out. On the inhale receive kindness; on the exhale offer strength held in check. Feel the balance point at the heart — beauty." },
        quiz: {
          q: "In the Tree's symbolism, what is “beauty” (Tiferet)?",
          options: ["Physical attractiveness", "The harmony that balances kindness and strength", "The highest sphere", "Wealth"],
          answer: 1,
          why: "Tiferet stands at the center of the Tree, harmonizing mercy and severity."
        }
      },
      {
        title: "Binah and Chokhmah — Understanding and Wisdom",
        teaching:
          "Above the heart shine two great lights: Wisdom, the lightning-flash " +
          "of insight that arrives whole and wordless, and Understanding, the " +
          "womb of thought that patiently gives the flash form. Meditation " +
          "cultivates both — the silence in which insight can strike, and the " +
          "steadiness that can hold it without shattering.",
        practice: { type: "timer", minutes: 8,
          guidance: "Sit with a genuine question you carry — hold it lightly, without reasoning. Let silence do the thinking. If nothing comes, the silence itself was the answer today." },
        quiz: {
          q: "How do Wisdom and Understanding differ in this teaching?",
          options: ["They are identical", "Wisdom is the wordless flash; Understanding gives it patient form", "Understanding comes first, then Wisdom", "Both are forms of memory"],
          answer: 1,
          why: "Chokhmah is the sudden seed of insight; Binah is the deep structure that develops it."
        }
      },
      {
        title: "Keter — The Crown of No-Thing",
        teaching:
          "At the summit of the Tree the Zohar places Keter, the Crown — so " +
          "far beyond concept that the mystics call it Ayin, No-Thing. It is " +
          "not emptiness as absence but as unlimited possibility, the way " +
          "silence is not the absence of music but its source. The highest " +
          "meditation lets go even of the ladder that led to it.",
        practice: { type: "timer", minutes: 10,
          guidance: "Release every technique. Sit as open awareness with no object at all. When the mind grasps for something to do, let even that go." },
        quiz: {
          q: "Why is the Crown called “No-Thing” (Ayin)?",
          options: ["Because it does not exist", "Because it is beyond all concepts — pure unlimited possibility", "Because it is forbidden to discuss", "Because it is empty of value"],
          answer: 1,
          why: "Ayin is fullness beyond form — the source from which every 'something' emerges."
        }
      },
    ],
  },

  /* ------------------------------------------------------------------ */
  {
    id: "yetzirah",
    title: "The Thirty-Two Paths",
    source: "Sefer Yetzirah — The Book of Formation",
    color: "#3fa7d6",
    icon: "🔤",
    intro:
      "The Book of Formation teaches that reality is spoken into being: ten " +
      "depths and twenty-two letters — thirty-two paths of wisdom. Breath, " +
      "sound, and number are not symbols of creation; they are its tools. " +
      "Here you practice with the raw materials: breath and vibration.",
    unlockedBy: { path: "zohar", stages: 2 },
    stages: [
      {
        title: "Ten Depths",
        teaching:
          "The Book of Formation counts ten depths: beginning and end, good " +
          "and evil, above and below, east, west, north, south. To meditate " +
          "is to stand at the center of all ten — the still point from which " +
          "every direction extends. The center moves with you; you are never " +
          "anywhere else.",
        practice: { type: "timer", minutes: 4,
          guidance: "Sense the space above you, below you, before, behind, left, right. Then sense yourself as the still center of all six directions. Rest there." },
        quiz: {
          q: "Where does the meditator stand in relation to the ten depths?",
          options: ["At the eastern edge", "At the still center from which all directions extend", "Below them", "Outside them"],
          answer: 1,
          why: "The practice places awareness at the unmoving center of all extension."
        }
      },
      {
        title: "The Breath of the Letters",
        teaching:
          "The letters, says the Book of Formation, were carved out of breath " +
          "and voice. Before any word there is exhalation; before meaning, " +
          "vibration. This is why breath-work sits at the root of so many " +
          "traditions: to return to the breath is to return to the moment " +
          "before the world was named — and to rest there is freedom.",
        practice: { type: "breath", minutes: 5, pattern: [4, 2, 6, 0],
          guidance: "Inhale 4, hold 2, exhale 6. On each long exhale, let a soft unvoiced “hhh” carry the breath out — the raw material of every word." },
        quiz: {
          q: "What lies before every word, according to this teaching?",
          options: ["Grammar", "Breath and vibration", "Writing", "Memory"],
          answer: 1,
          why: "Letters are carved from breath and voice — returning to breath returns you before naming."
        }
      },
      {
        title: "Sealing the Directions",
        teaching:
          "The Book of Formation describes the sealing of the six directions " +
          "of space with permutations of the divine name — a way of saying " +
          "that no direction of experience is outside the sacred. In practice " +
          "this becomes a protection meditation: whatever comes from any " +
          "quarter — memory, worry, sensation — arrives inside a sealed, " +
          "sacred space and cannot shake the center.",
        practice: { type: "timer", minutes: 6,
          guidance: "Mentally seal each direction — above, below, front, back, right, left — with a moment of attention and the felt sense: “this too is held.” Then sit within the sealed cube of space." },
        quiz: {
          q: "What does “sealing the six directions” mean in practice?",
          options: ["Locking the doors before meditating", "Recognizing no direction of experience is outside the sacred", "Facing east only", "Avoiding all sensation"],
          answer: 1,
          why: "Everything that arrives — from any quarter — is met inside sacred, held space."
        }
      },
      {
        title: "Three Mothers: Air, Water, Fire",
        teaching:
          "Three mother letters govern the elements: Aleph the air, Mem the " +
          "water, Shin the fire. In the body they are breath in the chest, " +
          "coolness in the belly, warmth in the head. The Book of Formation " +
          "teaches balance: fire above, water below, and air — the breath — " +
          "as the tongue of the scale between them.",
        practice: { type: "breath", minutes: 6, pattern: [5, 3, 5, 3],
          guidance: "Inhale (air) 5 — feel the chest; hold 3 — feel warmth (fire) at the head; exhale 5 — feel coolness (water) settle in the belly; hold 3 at the balance point." },
        quiz: {
          q: "In the doctrine of the three mothers, what balances fire and water?",
          options: ["Earth", "Air — the breath", "Metal", "Time"],
          answer: 1,
          why: "Aleph, the breath, is the tongue of the scale between Shin (fire) and Mem (water)."
        }
      },
      {
        title: "The Covenant of the One",
        teaching:
          "After all its combinations and permutations, the Book of Formation " +
          "ends where it began: the ten are one, the letters are one breath, " +
          "the paths are one path. Multiplicity is the alphabet; unity is " +
          "what is being said. The final practice of this path is to hear the " +
          "single word beneath the many.",
        practice: { type: "timer", minutes: 9,
          guidance: "Listen to the whole field of sound and sensation as one single event — not many things happening, but one thing happening in many ways." },
        quiz: {
          q: "What is the final teaching of the Book of Formation's paths?",
          options: ["The letters are ultimately many gods", "Multiplicity is the alphabet; unity is what is being said", "Only silence exists", "Numbers are superior to letters"],
          answer: 1,
          why: "All thirty-two paths resolve into one — the many are one speech."
        }
      },
    ],
  },

  /* ------------------------------------------------------------------ */
  {
    id: "serpent",
    title: "The Serpent Power",
    source: "The Serpent Power · Arthur Avalon (Sir John Woodroffe)",
    color: "#e4572e",
    icon: "🐍",
    intro:
      "Avalon's classic maps the yogic science of Kundalini: the coiled " +
      "energy resting at the base of the spine, and the six centers — chakras " +
      "— through which it rises to union at the crown. Here the game is " +
      "vertical: raise awareness, center by center.",
    unlockedBy: { path: "masters", stages: 3 },
    stages: [
      {
        title: "The Coiled One",
        teaching:
          "At the base of the spine, in the root center Muladhara, the " +
          "tradition pictures consciousness-energy coiled like a sleeping " +
          "serpent. She is not a metaphor for something else; in this science " +
          "she IS the power by which you think, feel, and live — operating at " +
          "minimum. Awakening is not adding energy but un-coiling what is " +
          "already there.",
        practice: { type: "chakra", minutes: 5, upTo: 1,
          guidance: "Attention settles at the base of the spine. Steady, warm, patient — like sitting quietly near something sleeping that you do not wish to startle." },
        quiz: {
          q: "What is Kundalini in this teaching?",
          options: ["A foreign energy you must acquire", "Your own consciousness-power, resting coiled at the base", "A breathing technique", "A deity outside you"],
          answer: 1,
          why: "The serpent power is the practitioner's own fundamental energy, ordinarily dormant."
        }
      },
      {
        title: "Roots and Waters",
        teaching:
          "The first two centers govern earth and water: solidity, safety, " +
          "fluidity, feeling. The texts insist the lower centers are not " +
          "'lower' in worth — a tree rises exactly as deep as its roots go. " +
          "Fear and craving met with awareness at these centers become " +
          "stability and creativity; ignored, they rule from the basement.",
        practice: { type: "chakra", minutes: 6, upTo: 2,
          guidance: "Breathe awareness into the root (base of spine), then the sacral center (below the navel). Ground first, then flow." },
        quiz: {
          q: "Why are the 'lower' chakras not inferior?",
          options: ["They are inferior", "A tree rises only as deep as its roots — they are the foundation", "They contain more energy", "They are easier to open"],
          answer: 1,
          why: "Ground and flow are the foundation on which every higher opening rests."
        }
      },
      {
        title: "The City of Fire",
        teaching:
          "The third center, Manipura at the solar plexus, is the city of " +
          "fire: will, digestion, transformation. Avalon's sources describe " +
          "it as the furnace where experience is metabolized. Untended, it " +
          "burns as anger and anxiety; tended, it becomes the steady heat of " +
          "purpose. The breath is its bellows.",
        practice: { type: "breath", minutes: 6, pattern: [4, 4, 6, 2],
          guidance: "Inhale 4 into the belly, hold 4 feeling warmth at the solar plexus, exhale slowly 6, rest 2. Stoke the fire evenly — never force it." },
        quiz: {
          q: "What is the practical sign of a well-tended solar-plexus fire?",
          options: ["Explosive anger", "Steady heat of purpose and will", "Coldness", "Sleepiness"],
          answer: 1,
          why: "The furnace transforms experience into purposeful energy rather than burning as anger or anxiety."
        }
      },
      {
        title: "The Unstruck Sound",
        teaching:
          "The heart center, Anahata, is named for the 'unstruck sound' — a " +
          "vibration not caused by two things striking, heard only in deep " +
          "stillness. Here the serpent's ascent changes character: below the " +
          "heart the work feels like effort; from the heart upward it feels " +
          "like love. Compassion is not an ornament of this yoga — it is the " +
          "engine of its upper half.",
        practice: { type: "chakra", minutes: 7, upTo: 4,
          guidance: "Rise center by center — root, sacral, solar plexus — and rest at the heart. Listen inwardly, as if for a sound that was never struck." },
        quiz: {
          q: "What does “Anahata” — the heart center's name — mean?",
          options: ["The struck drum", "The unstruck sound", "The red lotus", "The second gate"],
          answer: 1,
          why: "It names a vibration not produced by collision — audible only in profound stillness."
        }
      },
      {
        title: "Throat and Brow",
        teaching:
          "Vishuddha at the throat purifies expression: speech becomes " +
          "truthful and spare. Ajna at the brow, the center of command, is " +
          "where the two currents of the subtle body meet and the inner " +
          "teacher is heard. The texts say that at this center, instruction " +
          "no longer comes in words — you simply see.",
        practice: { type: "chakra", minutes: 8, upTo: 6,
          guidance: "Ascend through the five lower centers and rest attention gently between the eyebrows. Do not strain the eyes — the seeing here is not done with them." },
        quiz: {
          q: "What happens at Ajna, the brow center, according to the texts?",
          options: ["Speech becomes louder", "The two subtle currents meet and knowing becomes direct seeing", "Sleep begins", "Energy descends again"],
          answer: 1,
          why: "At the center of command, instruction arrives as direct insight rather than words."
        }
      },
      {
        title: "The Thousand-Petaled Union",
        teaching:
          "Above the six centers blooms Sahasrara, the thousand-petaled lotus " +
          "at the crown — not a seventh station but the destination: the " +
          "union of the rising power with pure consciousness. Avalon is " +
          "careful: this is not annihilation but completion, the wave " +
          "recognizing it was never other than the ocean. And the serpent, " +
          "having risen, gives the whole body-mind its blessing on the way " +
          "back down: an enlightened person still walks, works, and loves — " +
          "now luminously.",
        practice: { type: "chakra", minutes: 10, upTo: 7,
          guidance: "The full ascent: root to crown, unhurried. At the crown, release all technique and rest as open radiance. Then, deliberately, come back down center by center — return blessed, not spaced-out." },
        quiz: {
          q: "What is union at the crown compared to?",
          options: ["A wave annihilated by the ocean", "A wave recognizing it was never other than the ocean", "A serpent leaving the body", "A final sleep"],
          answer: 1,
          why: "Union is completion and recognition, not destruction — and it returns to bless ordinary life."
        }
      },
    ],
  },

  /* ------------------------------------------------------------------ */
  {
    id: "bardo",
    title: "The Clear Light",
    source: "The Tibetan Book of the Dead (Bardo Thödol)",
    color: "#9b5de5",
    icon: "🕉️",
    intro:
      "The Bardo Thödol — 'Liberation through Hearing in the Between' — is " +
      "read to the dying, but its real audience is the living: every moment " +
      "is a bardo, a between. Whatever appears, however beautiful or " +
      "terrifying, is the radiance of your own mind. Recognize it, and you " +
      "are free.",
    unlockedBy: { path: "masters", stages: 4 },
    stages: [
      {
        title: "Everything Is a Between",
        teaching:
          "The Tibetan teachers name six bardos, and only two concern death — " +
          "the others are this life, dream, and meditation. A bardo is any " +
          "gap: between breaths, between thoughts, between what just ended " +
          "and what has not yet begun. The gap is not empty waiting; it is " +
          "the open face of awareness itself, glimpsed before the next " +
          "concept covers it.",
        practice: { type: "timer", minutes: 4,
          guidance: "Attend to the gaps: the pause at the end of each exhale, the silence after each thought dissolves. Rest in the between." },
        quiz: {
          q: "What is a “bardo”?",
          options: ["Only the state after death", "Any between — a gap where open awareness can be glimpsed", "A Tibetan monastery", "A type of mantra"],
          answer: 1,
          why: "Death is only one bardo; this life, dream, and every gap between thoughts are betweens too."
        }
      },
      {
        title: "The Clear Light of the Ground",
        teaching:
          "At the moment of deepest letting-go, says the text, the Ground " +
          "Luminosity dawns — mind's own nature, clear light, empty and " +
          "radiant. It dawns for everyone; the difference between liberation " +
          "and wandering is only recognition. And recognition can be " +
          "rehearsed: every time you rest in bare awareness, you are " +
          "practicing for the one appointment nobody misses.",
        practice: { type: "timer", minutes: 6,
          guidance: "Let attention release every object and rest as sheer awake presence — lucid, clear, without center or edge. This resting IS the rehearsal." },
        quiz: {
          q: "What separates liberation from wandering when the clear light dawns?",
          options: ["Good luck", "Recognition — knowing the light as your own nature", "Ritual performed by others", "Physical strength"],
          answer: 1,
          why: "The luminosity dawns for all; those who recognize it as their own mind are freed."
        }
      },
      {
        title: "Peaceful and Wrathful Visions",
        teaching:
          "In the bardo of becoming, dazzling peaceful presences and " +
          "terrifying wrathful ones appear in turn. The text's refrain never " +
          "changes: do not be attracted, do not be afraid — these are your " +
          "own projections, your own mind wearing masks. In life it is the " +
          "same: praise and blame, hope and dread, all wear the face of the " +
          "one who watches.",
        practice: { type: "gaze", minutes: 6,
          guidance: "Sit and let thoughts come as visions — pleasant or harsh. At each one, tap the bell of recognition: “my own projection,” and let it dissolve. Neither chase nor flee." },
        quiz: {
          q: "What is the correct response to both beautiful and terrifying bardo visions?",
          options: ["Follow the beautiful, flee the terrifying", "Recognize both as your own mind's projections — neither attracted nor afraid", "Close your eyes", "Pray to the peaceful ones only"],
          answer: 1,
          why: "The refrain of the whole text: all appearances are the radiance of your own mind."
        }
      },
      {
        title: "Impermanence, the Kind Teacher",
        teaching:
          "The book was written for the dying because the dying finally pay " +
          "attention. But its wager is that you need not wait: to remember, " +
          "daily, that everything ends — this cup, this conversation, this " +
          "self-image — does not poison life but concentrates it. What is " +
          "held lightly can be loved fully; what is gripped is already lost.",
        practice: { type: "breath", minutes: 6, pattern: [4, 0, 8, 0],
          guidance: "Long releasing exhales: in 4, out 8. With every exhale, practice the small letting-go. The exhale is a rehearsal of grace." },
        quiz: {
          q: "How does remembering impermanence affect life, per this teaching?",
          options: ["It poisons enjoyment", "It concentrates life — what is held lightly can be loved fully", "It causes indifference", "It postpones love until later"],
          answer: 1,
          why: "Awareness of endings sharpens presence; gripping, not loss, is what kills love."
        }
      },
      {
        title: "Liberation Through Hearing",
        teaching:
          "The full title promises liberation through hearing — because in " +
          "the between, one reminder can be enough. Become that voice for " +
          "yourself: when fear rises in daily life, the trained mind hears, " +
          "as if read aloud by a friend: 'Recognize this as your own " +
          "radiance. You have rehearsed this. Rest.' The book you practiced " +
          "becomes the voice that finds you.",
        practice: { type: "timer", minutes: 10,
          guidance: "Rest in clear, open awareness. Occasionally offer yourself the reminder, silently: “Whatever appears is my own radiance.” Then return to silent resting." },
        quiz: {
          q: "What does the “hearing” in Liberation Through Hearing become, with practice?",
          options: ["A recording you must play", "Your own trained inner voice, reminding you in the moment of fear", "The voice of a lama only", "Background noise"],
          answer: 1,
          why: "Rehearsed reminders become self-arising: the practiced mind speaks the text to itself when needed."
        }
      },
    ],
  },

  /* ------------------------------------------------------------------ */
  {
    id: "being",
    title: "The Ocean of Being",
    source: "Science of Being and Art of Living · Maharishi Mahesh Yogi",
    color: "#2ec4b6",
    icon: "🌊",
    intro:
      "Maharishi's teaching is radical in its ease: Being — pure, silent, " +
      "blissful awareness — is not far away and hard to reach; it is the " +
      "nearest thing there is, and the mind dives toward it naturally when " +
      "allowed. Effort is the obstacle, not the method. Enlightenment is " +
      "normal life, established in Being.",
    unlockedBy: { path: "masters", stages: 5 },
    stages: [
      {
        title: "Being Is Not Far",
        teaching:
          "As the ocean's depth is silent beneath even stormy waves, the " +
          "mind's depth is silent beneath every thought. This silent basis is " +
          "called Being. The revolution in this teaching: the mind is not " +
          "dragged to silence by discipline — it sinks toward silence " +
          "spontaneously, because silence is more charming. Meditation is " +
          "letting, not forcing.",
        practice: { type: "timer", minutes: 5,
          guidance: "Sit comfortably, close the eyes, and do nothing at all. When thoughts come, let them; they are waves. Notice any moments the mind settles by itself — that settling needed no effort." },
        quiz: {
          q: "Why does the mind settle into silence, in this teaching?",
          options: ["Because it is forced by concentration", "Spontaneously — the mind is drawn toward the greater charm of Being", "Because thoughts run out", "It never settles"],
          answer: 1,
          why: "The mind moves naturally toward greater happiness; the silent depth is supremely charming, so effort is unnecessary."
        }
      },
      {
        title: "The Fine Art of Letting",
        teaching:
          "Trying to meditate is like trying to fall asleep — the trying is " +
          "the obstacle. The art is innocence: take a simple inward object " +
          "(the breath, or a soft inner sound), favor it gently, and when you " +
          "notice you've wandered, return without the least frown. Wandering " +
          "and returning is not failing at the process. It IS the process.",
        practice: { type: "timer", minutes: 8,
          guidance: "Favor the breath innocently — the way you'd listen to distant music. Wander, notice, return, all with zero self-criticism. Effortlessness is the technique." },
        quiz: {
          q: "What role does effort play in this style of meditation?",
          options: ["More effort brings faster results", "Effort is the obstacle; innocent favoring is the art", "Effort is needed only at the start", "Effort replaces the object"],
          answer: 1,
          why: "Like falling asleep, transcending happens when trying stops — the technique is gentle, innocent favoring."
        }
      },
      {
        title: "Water the Root",
        teaching:
          "To improve the leaves of a tree you do not paint them green — you " +
          "water the root. Maharishi's formula for living: regular dives into " +
          "Being (the root), then dynamic activity (the leaves). The dive " +
          "infuses the day with stability and creativity; the activity " +
          "stabilizes what the dive uncovered. Neither monastery nor " +
          "burnout: alternation is the art of living.",
        practice: { type: "breath", minutes: 6, pattern: [6, 0, 6, 0],
          guidance: "Slow even breath, 6 in / 6 out — a rhythm of alternation. Inhale: dive to the silent root. Exhale: rise into the day's activity. Rest and dynamism, one cycle." },
        quiz: {
          q: "What does “water the root to enjoy the fruit” mean here?",
          options: ["Gardening improves mood", "Contact Being in meditation, and all of life's activity improves from within", "Avoid activity entirely", "Fruit is a metaphor for wealth"],
          answer: 1,
          why: "Regular contact with the silent basis nourishes every branch of active life — no leaf-painting required."
        }
      },
      {
        title: "The Seven States",
        teaching:
          "Waking, dreaming, deep sleep — and a fourth: pure consciousness, " +
          "Being aware of itself alone. With practice the fourth stops " +
          "vanishing when you open your eyes: it coexists with waking, then " +
          "with dreaming and sleep — a fifth state, cosmic consciousness, " +
          "silence maintained alongside all activity. Two more refinements " +
          "and the teaching counts seven. Enlightenment here is not an exit " +
          "from life but ever fuller wakefulness inside it.",
        practice: { type: "timer", minutes: 10,
          guidance: "Meditate silently. In the last minute, open your eyes and continue sitting — practice letting the inner silence remain while seeing. Carry it with you when you stand." },
        quiz: {
          q: "What is “cosmic consciousness” (the fifth state)?",
          options: ["A trance replacing waking life", "Inner silence maintained alongside waking, dreaming, and sleep", "Deep sleep without dreams", "Astral travel"],
          answer: 1,
          why: "The fourth state, once stabilized, coexists with the other three — silence amid dynamism."
        }
      },
      {
        title: "Life Is Bliss",
        teaching:
          "The teaching's boldest claim: suffering is not the nature of life " +
          "but the symptom of losing contact with Being — and the remedy is " +
          "not philosophical but practical: dive, rise, act, repeat. A " +
          "person established in Being finds the same world transfigured; " +
          "nothing was added, everything was uncovered. 'Enlightenment,' in " +
          "the end, is simply normal human life — with the lights on.",
        practice: { type: "timer", minutes: 12,
          guidance: "A long, easy sit. No goal, no technique beyond innocence. You are not building enlightenment; you are un-covering what was never absent." },
        quiz: {
          q: "In this teaching, what is enlightenment?",
          options: ["Escape from the world", "Normal life with contact to Being fully established — the same world, uncovered", "A rare genetic gift", "Knowledge of scriptures"],
          answer: 1,
          why: "Nothing is added; established Being reveals the bliss that was always the nature of life."
        }
      },
    ],
  },
];

const CHAKRAS = [
  { name: "Muladhara",   en: "Root",         color: "#e63946", note: "base of the spine" },
  { name: "Svadhisthana",en: "Sacral",       color: "#f77f00", note: "below the navel" },
  { name: "Manipura",    en: "Solar Plexus", color: "#fcbf49", note: "solar plexus" },
  { name: "Anahata",     en: "Heart",        color: "#2a9d8f", note: "center of the chest" },
  { name: "Vishuddha",   en: "Throat",       color: "#3a86ff", note: "throat" },
  { name: "Ajna",        en: "Brow",         color: "#7209b7", note: "between the eyebrows" },
  { name: "Sahasrara",   en: "Crown",        color: "#b892ff", note: "crown of the head" },
];

const ACHIEVEMENTS = [
  { id: "first_sit",    icon: "🧘", name: "First Sitting",        desc: "Complete your first practice." },
  { id: "streak_3",     icon: "🔥", name: "Three-Day Fire",       desc: "Practice 3 days in a row." },
  { id: "streak_7",     icon: "🌅", name: "Week of Dawns",        desc: "Practice 7 days in a row." },
  { id: "streak_30",    icon: "🌕", name: "A Moon of Practice",   desc: "Practice 30 days in a row." },
  { id: "path_masters", icon: "🗝️", name: "Keeper of the Key",    desc: "Complete the Way of the Masters." },
  { id: "path_zohar",   icon: "🌳", name: "Climber of the Tree",  desc: "Complete the Tree of Splendor." },
  { id: "path_yetzirah",icon: "🔤", name: "Speaker of Letters",   desc: "Complete the Thirty-Two Paths." },
  { id: "path_serpent", icon: "🐍", name: "Serpent Riser",        desc: "Complete the Serpent Power." },
  { id: "path_bardo",   icon: "🕉️", name: "Friend of the Between",desc: "Complete the Clear Light." },
  { id: "path_being",   icon: "🌊", name: "Diver of the Ocean",   desc: "Complete the Ocean of Being." },
  { id: "hour_total",   icon: "⏳", name: "The First Hour",       desc: "Accumulate 60 minutes of practice." },
  { id: "five_hours",   icon: "🕰️", name: "Five Hours Still",     desc: "Accumulate 5 hours of practice." },
  { id: "quiz_perfect", icon: "🎓", name: "Ears That Hear",       desc: "Answer 10 reflections correctly." },
  { id: "all_paths",    icon: "🪷", name: "The Wisdom of the Ages", desc: "Complete every path. The teachings are one." },
];
