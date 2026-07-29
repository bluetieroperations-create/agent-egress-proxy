# The Teachings of the Masters 🪷

**The Wisdom of the Ages — a gamified meditation app.**

A single-page web app that turns a study of six classic works of spiritual
wisdom into a game of rising awareness. Read a short teaching, sit the
practice, answer a reflection, and watch your Light grow from **Seeker** to
**Enlightened**.

## The six paths

| Path | Source work | Focus |
| --- | --- | --- |
| 🗝️ The Way of the Masters | *The Book of the Masters* — R. Swinburne Clymer | Foundations: the divine spark, thought, motive, daily practice |
| 🌳 The Tree of Splendor | *The Zohar (The Book of Splendor)* | The hidden light, climbing the Tree of Life to the Crown |
| 🔤 The Thirty-Two Paths | *Sefer Yetzirah (The Book of Formation)* | Breath, sound, the directions of space, unity |
| 🐍 The Serpent Power | Arthur Avalon (Sir John Woodroffe) | Kundalini and the chakras, root to crown |
| 🕉️ The Clear Light | *The Tibetan Book of the Dead* | Bardos, recognition, impermanence, liberation through hearing |
| 🌊 The Ocean of Being | *Science of Being and Art of Living* — Maharishi Mahesh Yogi | Effortless transcending, the seven states of consciousness |

The foundation path (the Masters) is open from the start; the other paths
unlock as you progress through it, and each stage unlocks the next.

## Gameplay

- **Teachings** — each stage opens with an original paraphrase of a core idea
  from its source work.
- **Practices** — four interactive session types:
  - *Silent sitting* — a meditation timer with opening and closing bells.
  - *Breath practice* — a pulsing orb paces patterns like box breathing,
    4-7-8-style release breaths, and even 5-5 heart breathing.
  - *Chakra ascent* — the seven centers light up in sequence as the session
    ascends from root to crown.
  - *Awareness bells* — a mindfulness game: ring the bell each time you catch
    the mind wandering. Every tap is a victory, not a failure.
- **Reflections** — a short question after each practice to seal the teaching.
- **Progression** — earn **Light** (XP) for practice and reflection, climb
  eight ranks of awareness from Seeker to Enlightened, keep a daily **streak**,
  and collect 14 achievements, ending with *The Wisdom of the Ages* for
  completing every path.

Progress is saved in the browser (`localStorage`) — no account, no server,
no network calls.

## Running it

No build step, no dependencies. Either open the file directly:

```
open meditation-app/index.html
```

or serve the folder:

```
cd meditation-app
python3 -m http.server 8080
# then visit http://localhost:8080
```

## Notes

- All teaching text in the app is an original paraphrase and summary of ideas
  from the source traditions, written for this app — it is a companion to the
  books, not a replacement for reading them.
- This app is for practice and study; it is not medical or psychological
  advice.
