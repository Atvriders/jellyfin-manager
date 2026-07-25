# Medusa Super-Resolution Pass

**Date:** 2026-07-19 · **Status:** Approved (owner: "more real, super resolution, pop, lifelike, impressive")

Same creature, same behavior, same contract — dramatically better rendering. Iterate on the
shipped Medusa (`5a2d4e9`) inside `createJellyfield3D` only.

## Targets
- **Detail:** bell mesh to ~96×48; finer 8-lobe margin with lappet flutter (high-freq,
  low-amp rim wave); tentacles ~32 chains × ~24 nodes; richer oral-arm frill.
- **Optics:** two-pass bell (back faces then front — see the far wall of the bell through
  the near wall); wrap-light SSS keyed to the shafts; Blinn-Phong wet-glass specular;
  procedural mesoglea density + micro-wrinkles near the margin (fragment noise);
  3-sample spectral rim (real chromatic grazing split); gonad clover drawn with interior
  parallax (moves subtly against the shell = depth inside the body).
- **Pop:** soft-knee tonemap in-shader (bright core, never clips to white soup); layered
  aura billboards behind the bell; local background darkening behind the hero for
  contrast staging.

## Constraints (unchanged from the Medusa spec)
Public contract, selector, 2D engine, templates byte-untouched; edits only inside
createJellyfield3D. GLSL ES 1.00. Draw calls ≤10 (raised from 7 for the two-pass bell +
aura). No per-frame allocation beyond existing dynamic uploads. Context-restore must
rebuild every new resource. Dim band, mobile panel-probe anchor, hidden-pause, debounced
resize, DPR≤2, mount-false-on-failure all preserved. 110 tests green; SwiftShader +
--disable-webgl verification; zero console errors.

## Ascension addendum (owner: "god like look. celestial like.")

Divinity lives in the LIGHT, never the flesh — the creature stays a violet moon jelly;
the water treats her as sacred:

- **Crepuscular convergence:** the light shafts re-aimed to converge on the Medusa from
  the surface — she stands in a column of light, annunciation-style.
- **Aureole:** a diffuse glory/corona behind the bell — soft radiant ring with faint slow
  animated rays, never a hard halo.
- **Ascending star-motes:** tiny twinkling particles RISING around her (celestial dust,
  the inverse of marine snow), dense near her, sparse far away.
- **Apex star-core:** the bell's nucleus elevated to a star-like point with a subtle
  4-point sparkle.
- **Grade:** a restrained white-gold (#F2E4C0-family) permitted ONLY in the shafts,
  aureole, motes, and apex star — the creature's body remains violet/cyan.
- Budget: draw calls may rise to ≤12; all other constraints unchanged (GLSL ES 1.00,
  preallocation, context-restore covers new resources, dim band, mobile anchor,
  reduced-motion → 2D untouched).

## Seraph addendum (owner: "angel from the Diablo games… epic and god like")

She becomes an **angelic being** — the Angiris Council read: radiance, gold regalia,
and wings that are RIBBONS OF LIGHT, never feathers (Tyrael/Auriel). The moon-jelly
body survives underneath; the angel is what she *wears* and *emits*.

### Mandatory fixes (not optional aesthetics — these ship regardless)
1. **Arm attachment.** Tentacles/arms currently read as detached filaments floating
   near the bell. They must visibly GROW from the margin: thick roots tapering to fine
   tips (root width ≥3× current), a marginal collar/mantle where they emerge, root
   luminance matched to the bell rim so the tissue reads continuous, and roots tucked
   slightly UNDER the margin so they emerge from beneath rather than butting against it.
2. **Apex star clipping.** The star core blows to pure white on real GPUs. Tonemap it
   so the core stays luminous AND colored (gold, not white-out).
3. **Anatomy restored.** The aureole is washing out the gonad clover / canals won in the
   superres pass. Pull ambient glow back ~15% and lift interior-organ contrast so the
   photoreal anatomy reads through the divinity.

### Angelic layer
- **Ribbon-wings:** 6–10 long gold light-ribbons sweeping from the upper bell outward
  and upward in a wing silhouette — flowing, tapering, self-luminous, with brighter
  edges. Implemented as a new verlet chain kind (kind 2) so they ride the EXISTING
  ribbon draw call — no new draw call. They must read as wings at a glance, and stream
  with motion, not hang limp.
- **Gold regalia:** filigree accents at the margin/crown — restrained; the bell's flesh
  stays violet/cyan so she remains a jellyfish wearing divinity, not a gold blob.
- **Crown/halo:** the apex star resolved into a defined radiant crown.
- **Vertical presence:** the light column emphasized — High Heavens verticality.
- **Spirit wisps:** fine ascending light-trails shed from the wing tips.
- Interaction: `pulse()` (scan sonar) makes the wings FLARE and sweep — the angel
  reacting; `setActivity` drives their intensity.

### Budget/constraints
Draw calls ≤14. All prior constraints unchanged: edits only inside createJellyfield3D,
GLSL ES 1.00, preallocation (wing chains extend the existing preallocated node arrays),
context-restore covers new resources, dim band, mobile anchor, hidden-pause, DPR≤2,
reduced-motion → 2D untouched, 110 tests green, SwiftShader + --disable-webgl verified.

## Reliquary addendum (owner: "remove the wings and halo… angel LIKE, not an angel.
## gold on the arms, gold strips on the body, diamonds/rubies/emeralds on and around her")

She is no longer costumed as an angel. She becomes a PRECIOUS THING — gold-banded,
jewel-set, radiant. Angelic in quality, not in props.

### REMOVE (explicit angel iconography)
- The kind-2 gold ribbon-WINGS entirely (chains, shader branch, and their share of the
  ribbon buffers — reclaim the node budget).
- The bladed circlet CROWN and the mandorla ring behind her.
- Consequence to exploit: the wingtip guard in setupHero existed only to keep wings in
  frame and cost her real scale. With wings gone, SIMPLIFY it and give her presence back
  (target the pre-Seraph scale or better — she should feel monumental again).
- KEEP the ambient divinity: converging shafts, the soft glow, the light column, and her
  own nucleus. That is what "angel like" now means.

### ADD
- **Gold on the arms:** gold banding down the tentacles and oral arms — rings/ferrules at
  intervals, brightest at the root, thinning toward the tips. The arms should look
  adorned, not painted.
- **Gold strips on the body:** deliberate gold bands across the bell — meridian strips
  from apex toward the margin plus a banded rim — reading as goldsmithing laid ON the
  glass, following the bell's curvature (they must deform with the contraction wave, not
  slide over it).
- **Gemstones, set:** faceted stones set into the gold — diamond (prismatic white),
  ruby (deep red), emerald (green), sapphire (blue). On the bell along the strips and at
  intervals along the arms. Each must read as a CUT STONE: hard facets, a bright
  specular glint that moves with the view, colored depth — never a flat colored dot.
- **Gemstones, free:** the star-motes become drifting jewels — the same single instanced
  draw, now jewel-toned and faceted, twinkling as facets catch the light. Keep some
  plain gold dust for contrast.

### Discipline
Her flesh stays violet/cyan moon-jelly; gold and gems are ADORNMENT on and around her.
Gems must look precious, not like confetti or candy — restraint in count, richness in
each stone. If the frame reads as a Christmas tree, the pass has failed.

### Constraints
Draw calls ≤14 (should DROP with wings removed — report the count). All prior constraints
hold: edits only inside createJellyfield3D, GLSL ES 1.00, preallocation, context-restore
covers new resources, dim band, mobile anchor, hidden-pause, DPR≤2, reduced-motion → 2D
untouched, 110 tests green, SwiftShader + --disable-webgl verified, arms stay ATTACHED
(the root/collar/hem fix from the Seraph pass is permanent).
