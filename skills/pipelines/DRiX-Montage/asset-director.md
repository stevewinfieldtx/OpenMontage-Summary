# Asset Director - DRiX Montage Pipeline

## When To Use

This stage generates every visual and audio asset the final video needs: FLUX.1 Dev images, ElevenLabs Music v1 music tracks, and clean preacher audio clips extracted from the source sermon. The aesthetic quality of the final piece is won or lost here.

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Schema | `schemas/artifacts/asset_manifest.schema.json` | Artifact validation |
| Prior artifact | `state.artifacts["script"]` | Beat sheet: pull-quotes, scripture, motifs, audio cut markers |
| Prior artifact | `state.artifacts["scene_plan"]` | Per-beat visual treatment plan |
| Prior artifact | `state.artifacts["transcript"]` (via ingest) | Source for preacher audio clip extraction |
| Tools | `runware_image`, `runware_music`, `sermon_clip_extractor` | Generation + extraction |

## The DRiX Montage Aesthetic — Hard Rules

DRiX Montage uses a specific, locked-in aesthetic: **"cinematic nostalgia documentary."** Every asset produced by this stage must read as *memory*, not as media. References calibrated against: Photograph (Ed Sheeran), Mirrors (Justin Timberlake), Castle on the Hill (Ed Sheeran), Kids (MGMT), Midnight City (M83), Sweet Disposition (The Temper Trap).

The composition formula is 40% nostalgia / 30% music / 20% imperfect footage / 10% humor. This stage owns the nostalgia and the imperfect-footage proportions directly, and the music proportion through ElevenLabs Music v1 generation.

### Image Generation (FLUX.1 Dev on Runware)

Model: `runware:101@1`. Per-image cost ~$0.0045.

**Canonical style scaffold — append to every positive prompt:**

```
Kodak Portra 400 film stock, soft natural film grain, slight underexposure,
warm overcast or golden hour natural light, amateur snapshot taken quickly on a
point-and-shoot film camera, imperfect off-center framing, slightly off-balance
composition, natural skin texture with real human imperfections, lived-in moment,
mid-2000s photographic aesthetic.
NOT cinematic, NOT professional photography, NOT magazine quality,
NOT corporate, NOT retouched, NOT polished, NOT stock photo
```

**Subject vocabulary — strict 80/20 mix:**

80% **literal lived-in imagery** of universal spiritual experience as Millennials and Gen-Xers actually live it:
- A hand resting on a steering wheel at a red light, late afternoon
- A worn Bible on a kitchen counter next to a coffee mug
- An empty wooden pew shot from waist height
- A wedding ring on a bedside table
- A teenager's bedroom with a youth group flyer on a corkboard
- A hospital waiting room chair under fluorescent light
- Two hands held — one older, one younger — across generations
- A car driving home at sunset, suburban
- A folded paper bulletin on a passenger seat
- A church parking lot before service, half-empty, slanted light

20% **impressionistic abstract** (Kids/MGMT vocabulary):
- A blurred figure at the edge of a field at dusk
- Rain on a kitchen window with a blurred backyard
- Light through trees, dappled, dreamlike
- Out-of-focus crowd silhouettes

Used between literal beats to let the piece breathe.

**Never generate:**
- Religious iconography (stained-glass cathedral interiors, hands clasped in prayer stock poses, cross close-ups, dove imagery)
- Stained-glass cathedral grandeur of any kind
- Magazine-quality polished compositions
- Studio-lit anything
- Stock-photo poses or stock-photo aspirational scenes

**Hand discipline:** FLUX has known weaknesses on hand anatomy. For any image with prominent hands, **always generate 2-3 variants** and reject any with obvious anatomical errors (extra fingers, fused fingers, wrong proportions).

**Aspect ratios:**
- 1920x1080 for primary 16:9 deliverable
- 1080x1920 for 9:16 vertical cuts (when scene plan calls for them)
- 1024x1024 only for early test gens, never final

### Music Generation (MiniMax Music 2.6 on Runware — default workhorse)

Model: `minimax:music@2.6`. Fixed cost ~$0.15 per generation. **Cap is ~3 minutes per generation**, so a 6-10 min video needs 2-3 contiguous stems that the compose-director crossfades.

Music carries 30% of emotional weight. It is NOT background. Let it breathe.

**Upgrade path:** Switch to ElevenLabs Music v1 (`elevenlabs:1@1`) when MiniMax's lack of structural section control becomes the bottleneck. ElevenLabs Music supports a `compositionPlan` for section-by-section control at $0.40/min.

**Emotional arc — stem strategy with MiniMax:**
The script-director provides a 5-phase emotional arc: open contemplative → mid building → climax → resolution → CTA outro. With MiniMax's ~3 min cap, generate 2-3 stems that overlap these phases:
- Stem A (~3 min): open contemplative → early building
- Stem B (~3 min): late building → climax
- Stem C (~2-3 min, optional): resolution → CTA outro

Each stem prompt describes the emotional register for THAT stem, not the whole arc. The compose-director crossfades stems at phase transitions.

**Always set `instrumental=true`** (tool default) — preacher's voice clips carry the vocal content; competing vocals fight the piece.

**Reference-flavored prompts — always include at least one named-artist anchor:**
- Indie folk arc: `in the spirit of Bon Iver, Edward Sharpe, Sufjan Stevens, fingerpicked acoustic guitar, soft synth pad, slow build, hopeful melancholy`
- Cinematic indie electronic: `M83-style ambient build, glassy synths, drums entering at climax, ODESZA-style optimism but restrained`
- Restrained piano: `LANY-style restrained piano, atmospheric and breath-heavy, sparse arrangement`

**Never generate:** worship-band energy, anthemic Hillsong-style choruses, corporate uplift, generic Christian instrumental, melodic worship swells.

### Preacher Audio Clip Extraction (sermon_clip_extractor)

**Hard cap: 30-90 seconds total** across 3-4 clips.

Strategic placement:
- **Title declaration at open** (5-15s) — strongest single sentence in the sermon
- **Mid-piece anchor** (10-30s, optional) — one heart-stopping line that earns the climax
- **CTA at close** (10-25s) — the preacher's actual call-to-action language

Source: `.cache/youtube/{video_id}/audio.m4a`. The script-director identifies the candidate clip boundaries. This stage extracts them clean — no worship music or announcement bleed, with natural breath beats at start/end.

## Process

### 1. Read the script beat manifest
Every beat is one of: `pull_quote`, `scripture_overlay`, `moment`, or `audio_clip`. Build the generation queue from this.

### 2. Generate images per visual beat
For each `moment`, `pull_quote`, and `scripture_overlay` beat: construct subject sentence + canonical scaffold. Submit to `runware_image`. For hand-heavy beats, generate 2-3 variants. Store with full provenance (prompt, seed, model, timestamp, beat_id).

### 3. Quality-gate every image against the spec
Reject any output that looks magazine-polished, studio-lit, stock-photo-staged, religiously iconographic, or has obvious anatomical errors. Re-prompt and regenerate until passing. Log rejections in the decision log.

### 4. Generate music
One `runware_music` request matching the script's emotional arc, with named-artist references. Target duration matches script total runtime + 5s headroom. Store .wav under `projects/{project_id}/assets/music/`.

### 5. Extract preacher audio clips
From cached source audio, extract each script-identified clip with `sermon_clip_extractor`. Verify each is clean and naturally bounded. Store .wav under `projects/{project_id}/assets/preacher_audio/`.

### 6. Write asset_manifest
Reference every generated asset with file path, provenance, and the beat_id it serves.

## Quality Gate

- Every image conforms to the canonical scaffold (no professional/cinematic outliers)
- Hand-heavy images selected from 2+ variants with anatomy verified
- 80/20 literal/abstract mix respected across the full image set
- Zero religious iconography generated
- Music matches script's emotional arc and includes at least one named reference
- Music does NOT sound like worship band, Hillsong, or corporate uplift
- Preacher audio clips total 30-90s, clean of background bleed
- All asset files exist on disk with provenance recorded

## Common Pitfalls

- **FLUX defaulting to over-polished** when anti-prompt language is weak. Strengthen `NOT cinematic, NOT professional` and add specific anti-cues like `not studio lighting, not magazine`.
- **Generating religious iconography** instead of lived spiritual experience. Re-prompt with concrete domestic/secular subjects (kitchen, car, hospital, table).
- **Music gen producing anthemic worship-band output.** Add explicit anti-prompts: `not anthemic, not worship music, not Hillsong, not corporate Christian`.
- **Selecting too many preacher audio clips.** Cap is 30-90s TOTAL, not per-clip. Excess dilutes impact and pulls the piece toward "regular sermon clip."
- **Missing the 80/20 mix** and producing 100% literal imagery. Piece feels monotonous without the impressionistic 20%.
- **Skipping hand-variant generation** to save pennies. The pennies are not worth it — one bad hand in the final cut breaks the spell.

## Decision Log Categories

When logging decisions in this stage, use these categories:
- `image_rejected` — failed quality gate, regenerated
- `variant_selected` — chose v-N from multi-variant generation
- `music_prompt_revised` — initial gen failed tonal anchor, re-prompted
- `audio_clip_rejected` — clip had bleed or wrong bounds, re-extracted
- `motif_planted` — recurring visual motif first introduced at this beat
- `motif_payoff` — motif returned for emotional payoff at this beat
