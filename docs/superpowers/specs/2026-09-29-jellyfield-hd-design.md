# Jellyfield HD — super-resolution jellyfish, two-species bake-off

**Date:** 2026-09-29 · **Status:** shipped design. The bake-off is decided: the owner picked **candidate B, the purple-striped jelly** (see Outcome). This spec describes the final state; the bake-off sections are kept as the record of how it was chosen.

## Outcome

- **B, the purple-striped jelly (*Chrysaora colorata*), ships** (`app/static/jellyfield-species-striped.js`, species id `striped`). It is the one creature: there is no species switch.
- **Removed:** candidate A, the moon jelly (`jellyfield-species-moon.js` and its `<script>` tags), and the old WebGL1 3D engine (`createJellyfield3D` in `app/static/jellyfield.js`, with its selector path and the `?jelly=classic` frames).
- **The selector order is HD → 2D.** Reduced motion and no WebGL2 give the 2D field; an HD context that dies for good demotes to 2D. The 2D engine is byte-identical to what shipped before.
- **`?jelly` is gone** (one species). `?jellytier`, `?jellyslow`, `?jellydebug` and `?jellyldr` stay, for the tests.
- **E-S, inextensible chains** (engine change made at the finish). The Verlet chains could stretch: three constraint passes cannot hold a long chain taut, so B's 192-node arms grew +21 % (16 ms frames), +42 % (33 ms) and +49 % (50 ms) over 6 s, and the drape depended on frame rate and node count. A dynamic follow-the-leader pass now caps every link at its rest length (see Physics). B's lengths were re-tuned to the drape the owner judged: the arms from 3.0 (set to compensate for the stretch) to 3.6, which hangs 3.44 bell-local units deep at every frame rate, and the tentacles from 3.2 to 3.75, which hangs ~3.6 deep (the judged frames showed them stretched ×1.14).
- The contract surface that only A used, the `'fringe'` chain kind and `goldFerrules`, **stays**: it is documented contract, not dead code.

## Purpose

The page's background jellyfish should be a showpiece:
- sharp at every pixel density (1× desktop, 2× retina, 3× phones, 4K);
- genuinely beautiful rather than "CG";
- still a calm background that never hurts the legibility of the console on top of it.

The previous 3D engine (`createJellyfield3D` in `app/static/jellyfield.js`, now removed) had hit its ceiling:
- one forward pass straight to the canvas;
- no offscreen targets, so no refraction, bloom or light scattering;
- a 96×48 bell with visible ring banding, a kinked rim and a dark notch;
- organs that read as smudged decals and tentacles that read as hazy streaks;
- a 2× DPR cap.

## Decisions made during brainstorming

| Question | Decision |
|---|---|
| Same creature or new? | **Bake-off both.** A = the approved gold-banded moon jelly, rebuilt. B = a purple-striped jelly (*Chrysaora colorata*). The owner picks from real screenshots. **Picked: B.** |
| Approach | **New HDR multi-pass engine**, not a polish of the current one and not a raymarched volume. |
| Owner's standing aesthetic rules | Creature first. Divinity through light and restrained gold only: no wings, halo, crown or gemstones. White-gold appears only in light (shafts, glow, motes, apex star). The approved gold meridians, hem and arm ferrules were kept for candidate A; B's only gold is a fine highlight on the lappet edges. |

## Architecture

### Files

- **`app/static/jellyfield-hd.js`.**
  - Registers `window.JellyfieldHD = { create: createJellyfieldHD, species: {…}, params }`.
  - The engine object is `{ mount(canvas) -> bool, pulse(x, y, strength), setActivity(level), detach(), onFatal }`.
  - Also has a read-only `info()`, described under Test hooks.
  - It mounts the species registered under the id `striped`; without it, `mount` returns false and the selector falls through to 2D.
- **`app/static/jellyfield-species-striped.js`.** The one species: registers `window.JellyfieldHD.species.striped` (a no-op if the engine is missing).
- **`app/static/jellyfield.js`.** The 2D engine (unchanged) and the selector. The selector tries HD first, then 2D.
- **`app/templates/index.html` and `app/templates/login.html`.** Both load `jellyfield-hd.js`, then `jellyfield-species-striped.js`, *before* `jellyfield.js`. No other template changes.

`window.Jellyfield` keeps exactly `{ mount, pulse, setActivity }` plus the read-only `info()`. The scan console, Incoming panel and login page are untouched.

### Requirements and fallbacks

| Situation | What happens |
|---|---|
| **WebGL2 required** (`getContext('webgl2', {alpha:false, antialias:false, depth:false, stencil:false, premultipliedAlpha:false, powerPreference:'high-performance'})`) | No WebGL2, or any failure while building resources: `mount` returns false and the selector falls through to 2D on a fresh canvas. |
| `EXT_color_buffer_float` present | HDR targets are `RGBA16F`. |
| `EXT_color_buffer_float` absent | **LDR mode**: `SRGB8_ALPHA8` targets (sRGB storage keeps the dark water's 8-bit precision), a 0.25 pre-exposure in the scene shaders (undone in the composite), and dithering everywhere. |
| `prefers-reduced-motion` | The 2D field, as before, including live flips in both directions. |
| Context lost | The grace-then-demote behaviour. On restore, every GL object is rebuilt: programs, buffers, textures, framebuffers, renderbuffers and VAOs. A context still lost after 3 s of visible time demotes to 2D. |

### Frame passes

All shaders are **GLSL ES 3.00**, using VAOs and native instancing.

1. **Water** (render scale ×½, into texture `waterTex`, HDR, no MSAA). Contains:
   - the depth gradient and fog;
   - converging crepuscular shafts from the surface light;
   - distant silhouettes and rising star-motes;
   - sonar shells from `pulse()`;
   - the click-ripple glow.
2. **Scene** (render scale ×1, MSAA framebuffer: 4× Ultra, 2× High, none on Low).
   - A full-screen blit of `waterTex` (bilinear upsample).
   - The creature's back faces, then front faces, then tentacles, frills and fringe.
   - The bell fragment shader samples `waterTex` for **screen-space refraction**, offsetting by the view-space normal × thickness. Dispersion is 3 taps (R/G/B offsets). Fresnel mixes in reflection of a procedural surface light.
   - **Organs** are marched *inside* the bell. From the front-face hit, N steps go along the refracted ray toward the back face, accumulating the species' `organField` (emission plus absorption). This gives true parallax and depth. N is 12 / 8 / 5 by tier.
   - Alpha carries creature coverage (0–1), used by the god-ray pass.
   - The MSAA buffer is resolved with `blitFramebuffer` into `sceneTex`.
3. **God rays** (quarter resolution): a radial light-scattering march from the surface-light screen position. It samples `lightMask × (1 − coverage)`, so the bell **casts light-shadows** into the column. Samples are 64 / 40 / 24 by tier.
4. **Bloom:** threshold, then a dual-filter (Kawase) down chain (6 / 5 / 4 levels), then upsample-add.
5. **Composite** to the default framebuffer at `CSS × min(DPR, 3)`.
   - Adds `sceneTex`, bloom and god rays.
   - **Exposure and tone curve.** Highlights roll off into colour: the apex star stays gold, never white soup.
   - **Dim band**: the text column darkens by the same rule as before, fed by the measured `.column` rect.
   - Gentle vignette.
   - **Static hash grain** at ±0.005 display (about ±1.3 codes), per device pixel, which kills gradient banding. It is static, not animated: a moving field at that amplitude reads as TV static.
   - **Contrast-adaptive sharpen**, and an **FXAA-style edge pass on Low** (no MSAA).
   - When render scale < 1, the upsample happens here, followed by the sharpen.

### Resolution and quality tiers

The drawing buffer is `CSS size × min(devicePixelRatio, 3)`, capped at **8.3 MP** total (4K at 1×).

| Tier | Render scale | MSAA | Bloom levels | God-ray samples | Organ steps | Bell mesh | Chains |
|---|---|---|---|---|---|---|---|
| **Ultra** | 1.0 | 4× | 6 | 64 | 12 | 256×128 | full |
| **High** | 0.85 | 2× | 5 | 40 | 8 | 192×96 | full |
| **Low** | 0.7 | none (FXAA) | 4 | 24 | 5 | 128×64 | fewer tentacles (×0.6), fewer fringe instances; oral arms always all |

- **Starting tier:** phone-sized (`W ≤ 720`) or `deviceMemory ≤ 4` or `hardwareConcurrency ≤ 4` starts at **High**. Otherwise **Ultra**.
- **Adaptive stepping:** an exponential moving average of frame time.
  - EMA > 20 ms for 2 s: step **down**.
  - EMA < 11 ms for 10 s: step **up**.
  - At most one change per 8 s. Never above the starting tier's ceiling on phones.
  - Tier changes rebuild only size-dependent targets and meshes, never the whole context.
- Bell meshes use `Uint32` indices (WebGL2 core).

### Shared engine responsibilities (species-independent)

- **Camera and pointer:** perspective, ±3° mouse parallax, pointer ray. Same numbers as the old engine.
- **Layout:** `setupHero` is carried over — the desktop open-water beside `.column` and the phone band above the first `.panel`, both measured layout-relative, not scroll-relative. It is **driven by the species' `extent`** (the bell's swept envelope, not the drape) instead of hard-coded bell numbers.
- **Physics:** Verlet chains for every chain spec the species declares.
  - Chains are rooted on the species' margin (`marginJS`) or the oral disc.
  - Anchor spring, bob, lean and contraction wave are shared.
  - **Inextensible (E-S).** After the three Gauss-Seidel distance passes, one follow-the-leader pass from the pinned root moves any node whose link is longer than its rest length back onto it, so a chain is never longer than its rest length, whatever the frame rate or node count. It only shortens (a slack link keeps its sag). Its velocity term is load-bearing: follow-the-leader only ever pulls toward the root, so a correction left in the Verlet velocity is a one-way push that lifts and coils the arms within seconds. As in dynamic follow-the-leader (Müller, Kim and Chentanez 2012), each node's correction `d_k` is taken back out of its parent's velocity (`prev[k−1] += s·d_k`, damping factor `s` = 0.9: just under 1 leaves a tenth of each correction in the motion as damping; 1.0 and 0.9 hang within 2 % of each other, both frame-rate independent). Measured on B's arms after 6 s of sim: drawn length exactly `length` and drape depth 3.44 at 16, 33 and 50 ms frames (before: +21 / +42 / +49 % long). The tentacles likewise hang at exactly their own rest length (before: +1 / +4 / +12 %), which is the nominal `length` varied by up to ±16 % per tentacle (`setChainSegs`) so the margin does not read as a comb; oral arms are not varied.
  - All arrays are preallocated at mount; **zero per-frame allocation**.
- **Interaction:** `pulse()` shells, flare, whip, ripple and spring impulse; `setActivity()` shimmer. Same semantics and numbers as before.
- **Timing and precision:**
  - Pause while `document.hidden`.
  - Debounced resize at 150 ms.
  - **Bounded animation phases** (`timeS % period`, rates quantized to whole cycles), so fp32 stays precise over days.
  - The **no-`sin` value hash**, the mediump-safe one from the old engine, reused.

## The species contract (locked before any species code was written)

A species is a plain object registered in `window.JellyfieldHD.species[id]`. The engine reads it once at mount and at tier changes. The engine may rescale mesh and chain counts by tier. Species never touch GL. The engine mounts the species with id `striped`; the contract itself is species-agnostic.

```js
{
  id: 'striped',                  // the shipped species (A was 'moon')
  // swept envelope of the BELL in bell-local units (scale 1), incl. contraction
  // surge and flutter, not the drape; drives the layout
  extent: { halfWidth: Number, up: Number, down: Number },
  breath: Number,                 // seconds per contraction cycle (4.5)
  // bell-local height of the apex star (her nucleus, on the bell's up axis):
  // the apex at rest plus the shared 0.06 crown lift (B 0.96; A was 0.66)
  apexY: Number,
  palette: {                      // linear-RGB triples, HDR allowed (>1 = glows)
    body, rim, gold, glow, organ, filament
  },
  // JS mirror of bellShape at t = 1 (the margin), used to root the Verlet chains:
  // writes [x, y, z] in bell-local units into `out` (preallocated by the engine,
  // so no per-frame allocation) and returns it, for margin angle phi,
  // contraction wave 0..1 and the bounded flutter phase
  marginJS: function (phi, wave, flutPh, out) { /* ... */ },
  bell: {
    // GLSL ES 3.00 source injected into the shared bell shaders. Must define:
    //   vec3  bellShape(float t, float phi, float wave, float time)
    //         -> bell-local position; t: 0 = apex .. 1 = margin; phi: 0..2pi; wave: contraction 0..1
    //   float bellThickness(float t, float phi)        -> mesoglea thickness (refraction strength)
    //   vec4  bellSurface(float t, float phi, vec3 n, float time)
    //         -> rgb = surface tint/pattern, a = gold mask 0..1 (B: the lappet-edge gold)
    //         (B's visible stripe pigment and glass glow are rendered in organField at organEntry,
    //          the surface point, rather than here: an approved deviation, Task 7 fix round 1)
    //   vec4  organField(vec3 p, float time)
    //         -> rgb = emission, a = absorption, at bell-local point p (inside the body)
    // The organ march (the species may rely on exactly this): organField is called
    // only from the near-wall march in the bell fragment stage, in order, at
    // p = organEntry + organDir * organStep * (k + 0.5), k = 0 .. steps-1, where
    // organEntry is the near wall's surface point; rgb is integrated x organStep x
    // the transmittance so far, then T *= exp(-a * organStep). The globals
    // organDir / organStep / organPx / organEntry are set before the march.
    // Declared before this source (SPECIES_HEAD): CREATURE_T, FLUT_RATE, sq(),
    // vhash() / vnoise(), fw() (fwidth in the fragment stage, 0 in vertex stages).
    glsl: String
  },
  chains: [                        // any number of chain groups
    {
      kind: 'tentacle' | 'oralArm' | 'fringe',
      count: Number,               // Ultra count; engine reduces tentacles and fringe on Low
      nodes: Number,               // verlet nodes per chain ('fringe' is NOT verlet: see below)
      length: Number,              // bell-local units: the nominal length (chains are inextensible: oral arms hang at exactly it, each tentacle at it ±16 %)
      root: 'margin' | 'oralDisc', // where it attaches (margin uses marginJS)
      phase: Number,               // phi offset of chain 0
      width: [Number, Number],     // root and tip width, bell-local (filaments may go below 1 px: engine clamps to 1 px + coverage fade)
      frill: { amp: Number, freq: Number, across: Number } | null, // ruffled ribbon (oral arms); across = verts across the ribbon
      color: [r, g, b],            // linear RGB
      goldFerrules: Boolean        // gold ferrule bands along the strand (contract surface; A used it, B does not)
    }
  ],
  motion: { contraction: Number, rimFlutter: Number, sway: Number }
}
```

- **`fringe`** chains are *procedural instanced strands*, not Verlet. Each is a short hair-fine tentacle emerging from the margin, swayed in the vertex shader by the contraction wave and the bell's velocity. This is how candidate A got hundreds of marginal tentacles cheaply; B declares none, and the kind stays in the contract.
- **Filaments** (tentacles, fringe) are camera-facing strips with **analytic coverage**: width is clamped to ≥ 1 device pixel and alpha is scaled by `trueWidth / drawnWidth`. That keeps them crisp and alias-free at any DPR.
- **Oral arms with `frill`** are ribbons with `across` vertices across the width. The edge vertices are displaced by `amp × sin(freq × s + time)` along the ribbon normal (`s` = the node's position along the arm × `length`), and the shading is two-sided translucent.
  - With `across ≥ LACE_ACROSS` (6, an engine constant; candidate B's 18) the arm is a **lace curtain**: a sheet curled about the arm's axis whose arc length is the chain's `width`, spiralling slowly down the arm. `amp` ruffles its outer columns off the sheet's own normal at `freq`, each hem on its own phase, with soft hems and fine pleats shaded per pixel. A narrower frilled ribbon (candidate A's `across` 4) keeps the plain twist.

## Candidate A — the moon jelly, rebuilt (retired)

Built and refined for the bake-off, not picked, and removed. For the record:

- **Bell:** a shallow crystal-glass saucer with a faint violet body tint and an 8-lobed margin (analytic, no kinks).
  - A **fringe** of ~320 hair-fine marginal tentacles (fewer on Low).
  - 8 rhopalia: tiny soft points at the lobe notches.
- **Gold:** the meridians, hem and arm ferrules as *thin inlaid gold filaments* with a metallic specular.
- **Organs**, marched inside the body: a four-leaf clover of translucent lilac gonads with fine folded texture, and a crisp branching network of radial canals.
- **Oral arms:** 4 short, lacy arms, with frill on and a small amplitude.

## Candidate B — purple-striped jelly (*Chrysaora colorata*) — shipped

- **Bell:** a pale silver-violet glass dome.
  - 16 bold violet radial stripes fan from the apex and hold their weight down to the lappets (drawn at the surface point through `organField` / `organEntry`, see the contract).
  - A scalloped margin of 32 lappets.
  - Gold appears only as a fine highlight along the lappet edges, catching on the lit side.
- **Oral arms (the showpiece):** 4 long lace curtains, translucent pale rose-shell, slim at the mouth: 192 nodes, `length` 3.6 (hanging ~3.4 bell-local units deep, 3–4 bell lengths), `width` [0.20, 0.27], frill `{ amp 0.028, freq 22, across 18 }`.
- **Tentacles:** **24** long, maroon-rose, silk-fine marginal tentacles, three per octant, tapering out of the lappet clefts (three groups of 8; 44 nodes, `length` 3.75, hanging ~3.6 bell-local units deep). An earlier draft of this spec said 8; the species has 24, as the animal does.
- **Organs:** four soft, pleated gonads seen through the glass under the stripes. Less anatomy than A; the arms carry it.

The creature stands in the converging light column, with a soft glow behind it from the bloom and rising star-motes. The apex star stays gold. A scan press sends a sonar bloom through the creature; clicks ripple.

## Bake-off (done)

1. **Build order:**
   1. The shared engine was built first, with candidate A as its reference species.
   2. Candidate B was built against the locked contract in parallel with A's refinement.
2. **Refinement rounds.** Each candidate got **two** rounds. Each round captured:
   - full frames at 1440×900 @1×, @2×, 390×844 @3× and 3840×2160 @1×;
   - crops of the bell rim, the organs and the tentacles or arms.
3. **Measured judges** (Python, from the PNGs; `tools/jelly_capture.py` and `tools/jelly_judge.py`, which now capture the shipped species only):
   - **edge crispness:** mean gradient magnitude and half-peak width across the bell silhouette;
   - **banding:** distinct-step count and run length in smooth gradients;
   - **clipping:** % of pixels at channel 255;
   - **text contrast:** WCAG ratio behind the phone header text;
   - **cost:** frame time (SwiftShader, relative) and draw calls.
4. **Opinion judges** read the frames against the owner's rules above.
5. **Owner pick:** a private comparison page showed **the old engine, A and B side by side** at each size, plus close-ups. **The owner picked B.** The moon species and `createJellyfield3D` were then deleted.

## Test hooks and tests

- **`window.Jellyfield.info()`** returns
  `{ engine: 'hd' | '2d', species, tier, dpr, drawW, drawH, renderScale, hdr: Boolean, frames, heroBox }`.
  It is read-only and has no side effects. Every key is present whichever engine runs (null under 2D).
- **URL parameters, for tests only:**
  - `?jellytier=ultra|high|low` pins the tier.
  - `?jellyslow=<ms>` injects a synthetic frame cost so adaptive stepping is testable.
  - `?jellydebug=1` checks `gl.getError()` after every pass.
  - `?jellyldr=1` forces the LDR path.
  - The bake-off's `?jelly=moon|striped|classic` is removed: there is one species and no old engine.
- **Playwright e2e** (`app/tests/test_jellyfield_hd_e2e.py`; skips without a browser, like the other e2e files). Checks:
  - **Mounting:** HD mounts the `striped` species with **zero console errors**, and `gl.getError()` stays clean with the debug flag on (HDR and LDR).
  - **Fallbacks:** `--disable-webgl` gives 2D. Reduced motion gives 2D, and flips back to HD when it's turned off. A build failure in `mount` falls back to 2D cleanly. `WEBGL_lose_context` loss recovers on restore (also when restored while hidden), or demotes to 2D after the grace period. A failed rebuild on resize or tier change demotes to 2D.
  - **Pausing:** a hidden tab stops the RAF loop.
  - **Resolution:** the drawing buffer equals `CSS × min(DPR, 3)` within the 8.3 MP cap, at 1×, 2× and 3×.
  - **Adaptive quality:** `?jellyslow=40` steps the tier down, and it recovers once the slowdown is removed; the starting-tier rules; the tier pin.
  - **Memory:** JS heap growth over 60 s is under 2 MB. The canvas count stays 1.
  - **Image:** the water is banding-free and not speckled in LDR; the dim band leaves the backdrop alone; the creature is actually drawn.
  - **Layout:** the desktop places her beside the console; the phone keeps the apex on screen.
- `app/tests/test_jellyfield_glsl_static.py` (no browser, runs in CI) holds every `pow()` base in the engine and the species file syntactically non-negative, pins the organ-march contract statement by statement, and checks the species defines the contract.
- `app/tests/test_template.py` checks the script order: `jellyfield-hd.js`, then `jellyfield-species-striped.js`, before `jellyfield.js`.
- The CI JS syntax check covers the static files automatically.

## Out of scope

- A third species, or letting visitors switch species. After the pick there is one creature.
- Changes to the 2D fallback engine.
- WebGL1 support in the HD engine; those devices get the 2D field.

## Delivery

One commit after the owner's pick and the full verification: tests, e2e and a final adversarial review. The owner verifies smoothness on real devices, because the sandbox has only a software GPU.
