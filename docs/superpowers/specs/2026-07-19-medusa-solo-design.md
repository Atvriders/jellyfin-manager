# The Medusa — one ultra-quality jellyfish

**Date:** 2026-07-19 · **Status:** Approved

Replace the ~48-jellyfish 3D field with ONE monumental, anatomically-convincing jellyfish
("the Medusa") plus faint distant silhouettes deep in the fog. Owner's choices: solo hero +
faint silhouettes; **anchored centerpiece** behavior (breathes and sways in place in the open
water right of the panels; tentacles react to cursor and clicks).

## The creature
- Bell ≈64×36 parametric dome, 8-lobe scalloped margin, **traveling contraction wave**
  (pulse propagates apex→margin, ~4.5 s breath cycle) — not uniform squash.
- Internal anatomy: four-lobed gonad clover (moon-jelly signature), radial canals, nucleus.
- Shading: fresnel rim + wrap-light subsurface scattering keyed to the light shafts +
  subtle grazing-angle iridescence. Medusa-violet body, brand-cyan rim discipline kept.
- 4 frilled oral arms; ~24 fine marginal tentacles driven by **CPU verlet chains**
  (≈16 nodes each, preallocated) — inertia, drag, whip on bell kick.

## Behavior
- Anchored on a soft spring: desktop anchor = open water right of the column (midpoint of
  column-right-edge → viewport-right, y≈42%), recomputed on resize; mobile (≤720 px):
  anchor (50%, 30%), reduced scale, hero dim-floor raised to 0.55 (the band spans the full
  width there and must not bury the hero).
- Cursor: nearby tentacle nodes stir/curl toward the pointer ray; bell leans a few degrees.
- pulse(): impulse to the anchor spring + tentacle nodes + brightness flare; sonar shells
  unchanged; setActivity() unchanged (drives pulse rate + glow).
- Silhouettes: ~6 tiny instanced jellies (existing pipeline) at z −30..−38, heavily fogged.

## Unchanged (hard constraints)
Public `window.Jellyfield` contract; selector; the entire 2D engine (fallback + reduced-motion
users keep the shipped multi-jelly field — accepted aesthetic mismatch, deliberately not
touching reviewed fallback code); templates; backdrop/fog/shafts; dim band mechanics
(desktop). Budget: ≤7 draw calls/frame; no per-frame allocation except the tentacle ribbon
bufferSubData upload; all existing invariants (hidden-pause, debounced resize, DPR≤2,
context-loss protocol).

## Verification
110 pytest green; node --check; SwiftShader screenshots (idle/bloom/mobile/reduced-motion)
+ `--disable-webgl` fallback proof; zero console errors; preview re-inlined + republished.
