# Search Strategies, Anti-Deictic Translation & Templates

Reference guide for extracting reliable text solutions across genres and translating visual/video sequences into unambiguous text checklists.

---

## 1. Genre-Specific Search Strategies

| Genre / Scenario | Best WebSearch Queries | Best Sources to Pass to `fetch_text_guide.py` |
| :--- | :--- | :--- |
| **Survival Horror / Multi-Difficulty Puzzles** (*Silent Hill*, *Resident Evil*, *Alan Wake*) | `"<Game>" "<Puzzle Name>" all difficulties solution` | Dedicated fan wikis (SilentHillMemories, EvilResource), IGN, Game8 |
| **Action-Adventure / Shrines & Dungeons** (*Zelda*, *Tomb Raider*, *God of War*, *Indiana Jones*) | `"<Game>" "<Shrine/Dungeon Name>" walkthrough steps` | ZeldaDungeon, Polygon, Eurogamer, IGN, GameFAQs |
| **Soulslike / Boss Fights** (*Elden Ring*, *Hollow Knight*, *Sekiro*, *Lies of P*) | `"<Game>" "<Boss Name>" moveset dodge parry OR cheese site:reddit.com` | Fextralife / Official Wiki (weaknesses & phases) + Reddit threads (practical tells & openings) |
| **CRPG / Multi-Branch Quests** (*BG3*, *Witcher*, *Cyberpunk*, *Kingdom Come*) | `"<Game>" "<Quest Name>" walkthrough outcomes bug` | Official community wikis (`bg3.wiki`, `uesp.net`), RPGSite, Steam Community Guides |
| **Puzzle / Escape Room / Roguelite Puzzle** (*Portal*, *Talos Principle*, *Blue Prince*, *Outer Wilds*) | `"<Game>" "<Room/Puzzle Name>" exact solution OR rules site:steamcommunity.com OR site:reddit.com` | Steam Community Guides (`steamcommunity.com/sharedfiles/...`), AppUnwrapper, Neoseeker |
| **Metroidvania / Navigation Stuck Points** | `"<Game>" where to go after "<Boss or Ability>" site:reddit.com` | Reddit threads, Steam map guides, GameFAQs |
| **Indie / Mobile / New Releases** (Video-only coverage) | Run `fetch_yt_walkthrough.py --search "<Game> <Level> walkthrough guide"` | YouTube video chapters, pinned comments, transcripts, and `--save-storyboards` frame grids |

---

## 2. Anti-Deictic Translation Matrix (Turning Video Into Text)

Walkthrough videos and transcripts often use vague deictic language (*"go here"*, *"shoot that"*, *"align these two"*). Always convert those into self-contained spatial anchors:

| Vague Video Phrase | Translate Into Explicit Text Anchor |
| :--- | :--- |
| *"Put the coins in these slots"* | Number slots **1 to N from left to right** (or render a compact Markdown table). |
| *"Turn the dial to this symbol"* | Use **clock positions** (`12 o'clock`, `4 o'clock`) AND describe the symbol shape (`crescent moon`, `three-pronged fork`). |
| *"Flip the switches in this order"* | Label switches by **relative position from the entrance** (`Far-Left (1)`, `Center-Left (2)`, `Center-Right (3)`, `Far-Right (4)`). |
| *"Shoot the portal right up there"* | Anchor to **room geometry & lighting** (`the white panel on the upper-left wall directly above the exit door`). |
| *"Dodge when he does that attack"* | Describe the **visual/audio wind-up tell** + **dodge direction** (`When the boss raises both swords overhead with a red glint, dodge **into/behind** him after a 1-second delay`). |

---

## 3. Fixed vs. Procedural (RNG) Puzzles

Before giving a code or combination, verify whether it is **static** or **randomized per playthrough**:
- **Static Puzzles**: Provide the exact code/sequence upfront (including **Light / Standard / Hard** or **Original / Remake** variations in a table).
- **Procedural / RNG Puzzles**: State clearly at the top: *"Note: The exact code is randomized per playthrough; use this 3-step rule to read yours in ~15 seconds:"*, followed by the exact formula and a concrete example.

---

## 4. Stuck / Softlock Triage

When a user says *"I already did that step and nothing happened"*, check community comments (`site:reddit.com` or YouTube top comments) for:
1. **Missed Prerequisite Trigger**: Did they need to inspect a note/photo in their inventory first for the keypad/door to become interactive?
2. **Difficulty Differences**: Are slots or levers locked out on Light/Easy or rearranged on Hard?
3. **Known Progression Bug**: Does reloading the last checkpoint, fast-traveling away, or capping FPS fix a physics/script trigger?

---

## 5. Output Templates

### Template A: Level / Multi-Difficulty Puzzle Checklist

```markdown
**Starting Point:** [Room / Checkpoint Name] | **Required:** [Key Items / Abilities]

1. **Collect the required items** (skip if already in your inventory):
   - **[Item A]:** Inside [Location/Container].
   - **[Item B]:** Behind [Landmark].
2. **Interact with [Mechanism]** on the [North/East/etc.] wall and apply the solution for your difficulty:

| Step | Light / Easy | Standard / Normal | Hard |
| :--- | :--- | :--- | :--- |
| **Round 1** | Slot 1: **Man**, Slot 3: **Woman**, Slot 5: **Flower** | Slot 1: **Man**, Slot 3: **Woman**, Slot 5: **Flower** | Slot 1: **Man**, Slot 4: **Grave**, Slot 5: **Snake** |
| **Round 2** | Slot 1: **Sword**, Slot 3: **Woman**, Slot 5: **Snake** | Slot 2: **Sword**, Slot 4: **Woman**, Slot 5: **Flower** | Slot 1: **Grave**, Slot 2: **Sword**, Slot 3: **Flower** |
| **Round 3** | Slot 1: **Snake**, Slot 3: **Grave**, Slot 5: **Man** | Slot 3: **Man**, Slot 4: **Grave**, Slot 5: **Snake** | Slot 2: **Man**, Slot 3: **Woman**, Slot 5: **Snake** |

3. **Claim [Reward]** and exit through the **[Landmark] door**.

> **Watch Out:** Coins are double-sided—select a coin in the puzzle UI and press **Flip** to switch between **Man/Sword**, **Woman/Grave**, and **Snake/Flower**.
```

### Template B: Boss Fight Strategy Checklist

```markdown
**Boss:** [Boss Name] | **Weakness:** [Element/Type] | **Recommended Loadout:** [1-line setup]

### Phase 1 (100% – 50% HP)
1. **Overhead Slam (Red Glint):** Dodge **sideways** (shockwave travels straight forward) and punish with **2–3 hits**.
2. **3-Hit Spin Combo:** Block/parry the first two swings, then **dodge forward through the delayed 3rd swing** to attack from behind.
3. **Add Spawn:** Clear the **two ranged minions on the left ledge** immediately before resuming boss damage.

### Phase 2 (Below 50% HP)
1. **Arena-Wide Nova:** When the boss teleports to the center, sprint behind the **outer stone pillars** to break line-of-sight.
2. **Stagger Window:** After the nova finishes, the boss kneels for **6 seconds**—use your heaviest attack on the **exposed chest core**.

> **Watch Out:** Do not heal at mid-range while the boss is idle; it triggers an instant gap-closer lunge. Wait to heal right after dodging the **Overhead Slam**.
```
