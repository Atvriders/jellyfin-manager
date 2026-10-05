/* The purple-striped jelly (Chrysaora colorata): the bake-off's candidate B,
 * the owner's pick and the one creature that ships. Contract: spec "The
 * species contract", "Candidate B".
 *
 * A pale silver-violet glass dome, deeper than A's saucer, with sixteen bold
 * violet radial stripes fanning out from a hairline at the apex and holding
 * their weight down to the lappets (slightly wavy, as pigment laid down by
 * a living thing is); a scalloped margin of 32 lappets whose edges carry
 * the species' only gold, a fine highlight that catches on the lit side;
 * four soft, pleated gonads seen through the glass under the stripes;
 * twenty-four long, maroon-rose marginal tentacles, tapering out of the
 * clefts in threes; and the showpiece — four long lace curtains of oral
 * arms, translucent rose-shell, 3-4 bell lengths, slim at the mouth,
 * crimped and spiralling. Three tones, as the animal has: violet stripes,
 * pale rose curtains, maroon tentacles. Creature first; divinity through
 * light and restrained gold only: no wings, halo, crown or gemstones.
 *
 * ("A" in the notes below is the bake-off's other candidate, the moon
 * jelly, since retired: B was built against the same contract and keeps
 * A's shared motion.)
 *
 * ENGINE CONVENTIONS the GLSL relies on (the engine side is SPECIES_HEAD in
 * jellyfield-hd.js):
 *   wave  — the breath phase 0..1, one full contraction cycle per unit; the
 *           engine advances it at `breath` seconds a cycle, faster with
 *           activity. The travelling contraction is derived from it,
 *           ctr = max(sin(wave * 2pi - t * 2.2), 0) shaped, apex -> margin,
 *           exactly as A did, so the shared motion reads the same.
 *   time  — timeS % CREATURE_T (60 s): any rate on it must be a whole number
 *           of cycles per CREATURE_T (a multiple of 2pi/60 = 0.10471976), or
 *           the shape pops at the wrap. FLUT_RATE (4.18879, 40 cycles) is the
 *           flutter rate the engine also derives marginJS's flutPh from, so
 *           bellShape flutters with time * FLUT_RATE and the JS strand roots
 *           sit on the GLSL rim.
 *   vhash / vnoise, sq(x) — declared before this source. Squares use sq();
 *           every other pow() base is wrapped in max / abs / clamp, because
 *           pow() of a negative base is undefined in GLSL ES 3.00 (NaN on
 *           real drivers; SwiftShader hides it). A static test
 *           (test_jellyfield_glsl_static.py) holds this file to it.
 *   fw(x)  — fwidth in the fragment stage, 0 in the vertex stages (this
 *           source is compiled into the bell VS, the bell FS and the fringe
 *           VS, so it may only use what all three declare).
 *   organDir / organStep / organPx / organEntry — the organ march's
 *           footprint (direction, step, one screen pixel, and the near-wall
 *           surface point the march starts from; bell-local) and THE MARCH
 *           CONTRACT documented with them in SPECIES_HEAD: organField is
 *           called only from the near-wall march, in order, at
 *           p = organEntry + organDir * organStep * (k + 0.5), and its rgb
 *           is integrated x organStep, so a field that returns the
 *           segment's MEAN emission is integrated exactly, and pattern finer
 *           than a few pixels should fade rather than alias.
 *   apexY  — the contract's apex height: the engine hangs the apex star
 *           (her nucleus) there.
 *   marginJS(phi, wave, flutPh, out) — writes out[0..2], returns out; no
 *           allocation with the engine's preallocated `out` (a missing
 *           `out` returns a fresh array, for tests and tooling). */
(function () {
  'use strict';
  if (!window.JellyfieldHD) { return; }

  var BELL_GLSL = [
    /* THE DOME: deeper than A (th = t * 1.75 runs just past the equator,
       apex at +0.9), with A's travelling contraction and crown lift so the
       shared motion reads the same. THE LAPPETS: 32 rounded flaps on the
       outer 16% of the bell (lp), broad lobes and soft notches (lobe *
       (1.5 - 0.5 lobe) keeps the notch a smooth minimum, so the mesh and the
       finite-difference normals never see a kink); the lappet tips hang
       0.04 below the notches and swell slightly outward. The notch is kept
       GENTLE on purpose: the mesh puts a vertex exactly on every cleft
       (8 segments a lappet at Ultra), so a steep notch renders as a hard V
       at 4K; at 1.5 / 0.04 the turn per segment there is 40% less than the
       old 2 / 0.05. Each lappet bobs on the shared flutter rate. marginJS
       mirrors these lines exactly (the strand roots sit on this margin). */
    'vec3 bellShape(float t, float phi, float wave, float time) {',
    '  float wx = wave * 6.2831853 - t * 2.2;',
    '  float ws = sin(wx);',
    '  float ctr = max(ws, 0.0);',
    '  ctr = ctr * ctr * (0.55 + 0.45 * ctr);',
    '  float th = t * 1.75;',
    '  float r = sin(th);',
    '  float y = cos(th) * 0.90;',
    '  float sc = smoothstep(0.5, 1.0, t);',
    '  float lp = sq(smoothstep(0.84, 1.0, t));',
    '  float lobe = 0.5 + 0.5 * cos(32.0 * phi);',
    '  float lb = lobe * (1.5 - 0.5 * lobe);',
    '  r += 0.016 * lp * (lb - 0.5) * r;',
    '  y -= 0.040 * lp * lb;',
    '  float fl = sc * sc * sc;',
    '  y += 0.010 * fl * sin(32.0 * phi + time * FLUT_RATE);',
    '  r *= 1.0 - 0.22 * ctr * smoothstep(0.1, 0.9, t);',
    '  y += 0.06 * ctr * (1.0 - smoothstep(0.0, 0.7, t));',
    '  y -= 0.13 * ctr * sc;',
    '  return vec3(r * cos(phi), y, r * sin(phi));',
    '}',
    /* a deep crown of mesoglea thinning to fine lappets: the crown figure is
       what lets the organ march (1.8 x thickness) reach the gonads under it */
    'float bellThickness(float t, float phi) {',
    '  return mix(0.72, 0.12, smoothstep(0.15, 1.0, t));',
    '}',
    /* ------------------------------------------------------ THE STRIPES */
    /* Sixteen radial stripes, one on every other lappet. d = the arc
       distance (bell-local, at the dome's rest radius sin(1.75 t)) from the
       nearest stripe's centreline, which wanders a little as it runs down
       (each stripe on its own phase: laid down, not ruled). hw = its half
       width: a hairline at the apex, so the sixteen never merge into a cap,
       fanning out to a bold band over the shoulder (a third of the spacing)
       and on, 15% wider, down the lower bell (C. colorata's stripes broaden
       toward the margin), narrowing only on the lappet itself (t > 0.88),
       where the stripe ends. The widening stays clear of the measured
       outline: the 4K rim width went 1.901 -> 1.866 px with it. */
    /* t is CLAMPED: under MSAA the fragment stage runs at the pixel centre,
       which on a silhouette pixel can lie outside the triangle, so the
       interpolated t extrapolates a little below 0 at the apex; sin() of it
       went negative, d < 0, and the spine factor below (d / hw with hw = 0
       there) blew up to a huge negative m — mix(pale, violet, m) then
       extrapolated into a green-white firefly (linear (3.0, 5.4, 2.8)) on
       the crown's silhouette (Task 7 fix round 1, item 3).
       Two more outputs for the skin's pigment fibres: sx, the SIGNED angle
       from the stripe's (wandering) centreline, and sid, the stripe's index
       0..15. sid (not k) keys the fibre noise, so it is continuous across
       the 0 / 2pi wrap that runs down the middle of stripe 0 (k = 0 and
       k = 16 there are the same stripe). bellSurface ignores both. */
    'void stripeGeom(float t, float phi, out float d, out float hw, out float sx, out float sid) {',
    '  t = clamp(t, 0.0, 1.0);',
    '  float k = floor(phi / 0.3926991 + 0.5);',
    '  float sd = mod(k, 16.0);',
    '  float wav = 0.024 * sin(t * 9.0 + sd * 2.1) * smoothstep(0.08, 0.6, t);',
    '  float dphi = phi - k * 0.3926991 - wav;',
    '  sx = dphi;',
    '  sid = sd;',
    '  d = abs(dphi) * sin(min(t * 1.75, 1.5707963));',
    '  hw = 0.050 * smoothstep(0.02, 0.50, t) * (1.0 + 0.15 * smoothstep(0.50, 0.85, t) - 0.30 * smoothstep(0.88, 0.99, t));',
    '}',
    /* the stripe's coverage: a crisp edge antialiased over aa (one screen
       pixel, so it is a clean line at any DPR), pigment a little denser
       along the stripe's spine */
    'float stripeMask(float d, float hw, float aa) {',
    '  float m = 1.0 - smoothstep(hw - aa, hw + aa, d);',
    '  return m * (0.80 + 0.20 * (1.0 - sq(clamp(d / max(hw, 1e-4), 0.0, 1.0))));',
    '}',
    /* THE SURFACE (rgb tints the water seen through the glass; a = gold):
       pale silver-violet glass, the stripes violet. The gold is a HAIRLINE
       OF LIGHT on the lappet edge, the species' ONLY gold:
       - its width follows the screen (hwg ~ 0.75 px of t, clamped), so it
         is ~1.5 px at any DPR instead of a fixed band that read as a 3-4 px
         khaki pencil outline at 4K (the hem faces down, so only the
         engine's ambient floor lights it: a wide line there is dull);
       - it is fullest on each rounded lappet and fades into the clefts
         (sq(lobe)), where the mesh's corners were most visible;
       - it is LIGHT CATCHING AN EDGE, not an outline: lsd follows the key
         light's horizontal direction (L.xz normalised = (0.8829, 0.4694)),
         so the line glints on the lit right-front lappets, the same side as
         the dome's gleam, and falls to 12% on the far side of the light
         (measured at 4K: the hem's lift over the glass, left 26.9 -> 5.0,
         right 25.2 -> 22.7); a slow caustic shimmer (6 per turn, 4 cycles
         per CREATURE_T) moves along it +-10%. palette.gold is unchanged, so
         there is no new peak;
       - it is drawn on the NEAR wall only. n arrives flipped toward the
         eye, so it points outward on the near wall and inward on the far
         wall seen from inside; u_m has no yaw (lean <= 0.45, roll <= 0.2
         rad), so its horizontal part against the radial direction is about
         +-0.9 and a robust wall test. Seen through the near glass, the far
         wall's gold (its emission at 45% and its coverage raised by
         gold x 0.7) read as a stitched seam across the middle of the bell. */
    'vec4 bellSurface(float t, float phi, vec3 n, float time) {',
    '  float d, hw, sx, sid;',
    '  stripeGeom(t, phi, d, hw, sx, sid);',
    '  float m = stripeMask(d, hw, fw(d) * 0.7 + 1e-4);',
    '  vec3 tint = mix(vec3(0.96, 0.94, 1.05), vec3(0.52, 0.26, 0.82), m);',
    '  float hwg = clamp(fw(t) * 0.75, 0.0006, 0.0024);',
    '  float aah = fw(t) * 0.7 + 1e-4;',
    '  float edge = 1.0 - smoothstep(hwg - aah, hwg + aah, abs(1.0 - hwg - t));',
    '  float lobe = 0.5 + 0.5 * cos(32.0 * phi);',
    '  vec2 rd = vec2(cos(phi), sin(phi));',
    '  float near = smoothstep(0.05, 0.35, dot(n.xz, rd));',
    '  float lsd = smoothstep(-0.30, 0.85, dot(rd, vec2(0.8829, 0.4694)));',
    '  float shim = 0.80 + 0.20 * sin(phi * 6.0 - time * 0.4188790);',
    '  return vec4(tint, edge * near * (0.08 + 0.82 * sq(lobe)) * (0.12 + 0.88 * lsd) * shim);',
    '}',
    /* ---------------------------------------------------- organ helpers */
    /* erf (Winitzki, |error| < 1.3e-4) and the exact mean of a Gaussian over
       a march segment: what lets a thin sheet of tissue be integrated across
       a step instead of hit or missed (as A does) */
    'float erfA(float x) {',
    '  float x2 = x * x;',
    '  float e = exp(-x2 * (1.2732395 + 0.147 * x2) / (1.0 + 0.147 * x2));',
    '  return sign(x) * sqrt(max(1.0 - e, 0.0));',
    '}',
    'float gaussMean(float x0, float h, float s) {',
    '  if (h < 0.02 * s) { return exp(-sq(x0 / s)); }',
    '  return 0.8862269 * s / (2.0 * h) * (erfA((x0 + h) / s) - erfA((x0 - h) / s));',
    '}',
    /* THE SKIN — and a RULED DEVIATION from the contract's wording: B's
       visible stripes (and the silver-violet glow of the glass between
       them) are rendered HERE, in organField, at organEntry, not in
       bellSurface. bellSurface's rgb can only tint the water seen through
       the glass; the engine's own body light is one colour over the whole
       dome and washed stripes drawn there into a grey veil (measured
       (58, 49, 77) on (91, 92, 112)). bellSurface still carries the same
       stripes as that tint, so the far wall seen through the near one shows
       them faintly; the bold pigment is this skin. (Controller ruling,
       Task 7 fix round 1: approved.)
       The skin is bound to the SURFACE through the engine's march contract
       (SPECIES_HEAD in jellyfield-hd.js): organEntry is the near-wall point
       the march starts from and the samples come in order at depth
       dot(p - organEntry, organDir) = (k + 0.5) x organStep. So the first
       sample — the one whose segment begins at the surface — evaluates the
       stripes at organEntry itself, exactly once per fragment, instead of
       at the interior midpoints, where a radial stripe lies at a different
       angle on every step and smears along the flanks. Its light is returned
       divided by the step, so it adds the same radiance at every tier; its
       pigment is an absorption the rest of the march sees (the gonads glow
       through the glass, dimmed under a stripe). */
    /* value noise that repeats every P cells in x: the skin's radial grain
       runs round the bell on phi x 720 / 2pi, so the wrap at phi = 0 / 2pi
       (down the middle of stripe 0) has no seam */
    'float pnoise(vec2 p, float P) {',
    '  vec2 i = floor(p), f = fract(p);',
    '  f = f * f * (3.0 - 2.0 * f);',
    '  float x0 = mod(i.x, P), x1 = mod(i.x + 1.0, P);',
    '  return mix(mix(vhash(vec2(x0, i.y)), vhash(vec2(x1, i.y)), f.x),',
    '             mix(vhash(vec2(x0, i.y + 1.0)), vhash(vec2(x1, i.y + 1.0)), f.x), f.y);',
    '}',
    /* ------------------------------------------------------ THE ORGANS */
    'vec4 organField(vec3 p, float time) {',
    '  float h = organStep * 0.5;',
    '  vec3 em = vec3(0.0);',
    '  float ab = 0.015;',
    '  if (dot(p - organEntry, organDir) < organStep) {',
    '    vec3 e = organEntry;',
    /* the entry point's (t, phi) on the rest dome — r = sin 1.75t,
       y = 0.9 cos 1.75t, so th = atan(r, y / 0.9) — and the dome's normal
       there (the ellipsoid's gradient): the contraction moves the point a
       little, the pattern goes with it */
    '    float er = length(e.xz);',
    '    float et = clamp(atan(er, e.y / 0.9) / 1.75, 0.0, 1.0);',
    '    float ephi = atan(e.z, e.x + 1e-5);',
    '    ephi += ephi < 0.0 ? 6.2831853 : 0.0;',
    '    float d, hw, sx, sid;',
    '    stripeGeom(et, ephi, d, hw, sx, sid);',
    /* PIGMENT LAID IN TISSUE, not a vector fill: the stripe's edge is
       feathered to at least 0.0028 bell-local (the old one-pixel edge at
       1x; ~2.7 px soft at 4K, where a razor edge read as CG), and the
       pigment is streaked with fine radial fibres (noise cells 1/150 rad
       across, 1/12 of t along, keyed by sid so stripe 0 is seamless across
       the wrap) that fade out (fibA) before a cell is ~1.3 px, so they
       show at @2 and 4K and never alias. Two more marks of a living hand,
       under the same fade: each EDGE of each stripe wanders on its own
       (hw +-8% on noise 30 per unit of t, keyed by stripe and side), 1-3 px
       at 4K, and each stripe carries its own pigment density (+-10%, hashed
       on sid): no two alike, none ruled. */
    '    float fibA = 1.0 - smoothstep(0.35, 0.75, organPx * 150.0 / max(er, 0.05));',
    '    hw *= 1.0 + 0.16 * fibA * (vnoise(vec2(sid * 17.0 + step(0.0, sx) * 5.0, et * 30.0)) - 0.5);',
    '    float m = stripeMask(d, hw, max(organPx * 0.7, 0.0028) + 1e-4);',
    '    m *= 1.0 - 0.18 * fibA * vnoise(vec2(sx * 150.0 + sid * 13.0, et * 12.0));',
    '    m *= 0.90 + 0.20 * vhash(vec2(sid, 7.3));',
    '    vec3 ne = normalize(vec3(e.x, e.y / 0.81, e.z));',
    /* lit from above (the surface light, bell-local up to the lean); the
       glass glows more where the eye looks through more of its skin, so the
       dome reads as a lit volume rather than a painted shell. Two more
       terms make it read as a VOLUME OF JELLY lit from the crown, not an
       evenly glowing lampshade:
       - light entering at the crown, where the shaft and the apex star land
         (exp over et 0.32), weighted by k so it stays off the grazing
         silhouette ramp (the outline width is gated);
       - the mesoglea thinning toward the lappets (et 0.35 -> 0.95), so the
         lower bell clears toward the grazing flanks.
       The glass is silver-VIOLET (R/G 1.29; the old one was R = G, a
       neutral blue-grey between the stripes), ~5% lower in luminance.
       THE LOWER BELL STAYS STRIPED: the engine adds the far wall's glow and
       the sky's reflection to every pixel of the near wall, a floor the
       species cannot dim; as lt falls down the bell that floor greyed the
       pigment out and the stripes faded before the margin. Where the eye
       looks squarely through the skin (kf: k 0.30 -> 0.80) the clearing is
       cut to 40% (glass 58 -> 67 sRGB at 4K, y 1150) and the stripe's own
       light is cut by up to 60% down the lower bell (pig), so each stripe
       stays a dark band to the lappets, as C. colorata's do. Both are
       weighted by kf, so they do nothing on the grazing outline (gated). */
    '    float dl = max(dot(ne, vec3(0.28, 0.95, -0.13)), 0.0);',
    '    float k = abs(dot(organDir, ne));',
    '    float kf = smoothstep(0.30, 0.80, k);',
    '    float path = 1.0 / (0.25 + 0.75 * k);',
    '    float lt = (0.22 + 0.78 * dl) * path',
    '             * (1.0 + 0.40 * k * exp(-sq(et / 0.32)))',
    '             * (1.0 - 0.35 * smoothstep(0.35, 0.95, et) * (1.0 - 0.6 * kf));',
    '    vec3 glass = vec3(0.72, 0.56, 1.00) * 0.25;',
    '    vec3 pig = vec3(0.55, 0.12, 1.00) * 0.17 * (1.0 - 0.6 * kf * smoothstep(0.40, 0.92, et));',
    /* MESOGLEA, not CG glass, in the glass between the stripes only:
       - a living mottle (+-12% density), triplanar on the dome's normal so
         it has no seam or pole, faded out by organPx before a cell is
         ~1.7 px and weighted by k (off the outline);
       - a fine RADIAL GRAIN (+-9%): 720 cells round the bell and 26 down
         it, about 6 x 45 px at 4K and 4 px across at @2, periodic in phi
         (pnoise), fading out (rA) from 4 px cells and gone at 2 px — so at
         1440x900@1, where the lower bell's cells are 1.8-2.3 px, it is
         gone and nothing crawls as she moves — and weighted by k. */
    '    vec3 w3 = ne * ne;',
    '    float gA = (1.0 - smoothstep(0.30, 0.60, organPx * 70.0)) * k;',
    '    float gr = w3.y * vnoise(e.xz * 70.0) + w3.x * vnoise(e.zy * 70.0 + 7.1) + w3.z * vnoise(e.xy * 70.0 + 3.3);',
    '    float cw = 6.2831853 * max(er, 0.02) / 720.0;',
    '    float rA = (1.0 - smoothstep(0.25, 0.50, organPx / cw)) * k;',
    '    float rg = pnoise(vec2(ephi * 114.59156, et * 26.0), 720.0);',
    '    em += mix(glass * (1.0 + 0.24 * gA * (gr - 0.5) + 0.18 * rA * (rg - 0.5)), pig, m) * lt / organStep;',
    '    ab += m * 1.0 / organStep;',
    '  }',
    /* ---- the gonads: four soft, gently folded lobes hanging under the
       crown (Chrysaora's are frilled curtains of tissue round the stomach),
       interradial to the oral arms (the arms root at pi/4 + k pi/2, the
       gonads at k pi/2: one faces the eye, two sit on the flanks — two
       lobes side by side at +-45 degrees read as a face), a subtle
       lilac-rose glow seen through the glass and dimmed under the stripes.
       Each is an anisotropic Gaussian — long round the bell, short in, out,
       up and down — in its own frame
       (radial, up, tangential), and its mean over this march segment is
       exact (A's blob(), stretched): in the Gaussian's unit coordinates the
       segment is a straight line, so the part along it is gaussMean and the
       part across it is exp. A thin slab seen from the presentation tilt
       read as two smears; a volume reads as a glow. Only the nearest lobe
       is evaluated, and only when the segment can reach it. The lobes are
       kept narrow round the bell (0.15) so a gap opens at each arm root,
       and PLEATED: fold = sin^4 at 6 per unit with a wandering crease, so
       each lobe shows three or four narrow crests of folded tissue that
       catch the light, with dark troughs between (0.10 floor), not soft
       wisps of smoke. The gain (2.7) keeps the mean emission of the old
       sin^2 fold (0.4375 x 2.7 ~ 0.575 x 2.1) with crests 29% brighter
       (~140 sRGB at most, far from clipping). y stays 0.30: the march
       reaches the lobes only from the thick crown. */
    '  float rr = length(p.xz);',
    '  if (abs(p.y - 0.30) < 0.26 + h && rr < 0.70 + h) {',
    '    float ga = floor(atan(p.z, p.x + 1e-5) / 1.5707963 + 0.5) * 1.5707963;',
    '    vec2 er = vec2(cos(ga), sin(ga));',
    '    vec3 dd = p - vec3(er.x * 0.30, 0.30, er.y * 0.30);',
    '    vec3 sg = vec3(1.0 / 0.080, 1.0 / 0.060, 1.0 / 0.15);',
    '    vec3 q = vec3(dot(dd.xz, er), dd.y, dd.z * er.x - dd.x * er.y) * sg;',
    '    vec3 w = vec3(dot(organDir.xz, er), organDir.y, organDir.z * er.x - organDir.x * er.y) * sg;',
    '    float wl = max(length(w), 1e-4);',
    '    float al = dot(q, w) / wl;',
    '    float g = exp(-max(dot(q, q) - al * al, 0.0)) * gaussMean(al, wl * h, 1.0);',
    '    float fold = 0.10 + 0.90 * sq(sq(sin(q.z * 6.0 + 1.2 * sin(q.x * 2.5 + q.y * 1.5) + time * 0.2094395)));',
    '    em += vec3(0.90, 0.50, 0.96) * g * fold * 2.7;',
    '    ab += g * 0.5;',
    '  }',
    '  return vec4(em, ab);',
    '}'
  ].join('\n');

  /* bellShape at t = 1 (sin(1.75) = 0.983986, cos(1.75) * 0.90 = -0.160421;
     lp = sc = 1, the crown lift is 0 there), with the attachment tuck the
     engine expects: the roots sit just under the rim */
  function marginJS(phi, wave, flutPh, out) {
    var o = out || [0, 0, 0];
    var wsm = Math.sin(wave * 6.2831853 - 2.2);
    var ctr = wsm > 0 ? wsm * wsm * (0.55 + 0.45 * wsm) : 0;
    var r = 0.983986;
    var y = -0.160421;
    var lobe = 0.5 + 0.5 * Math.cos(32 * phi);
    var lb = lobe * (1.5 - 0.5 * lobe);
    r += 0.016 * (lb - 0.5) * r;
    y -= 0.040 * lb;
    y += 0.010 * Math.sin(32 * phi + flutPh);
    r *= 1 - 0.22 * ctr;
    y -= 0.13 * ctr;
    r *= 0.936;
    y -= 0.052;
    o[0] = r * Math.cos(phi); o[1] = y; o[2] = r * Math.sin(phi);
    return o;
  }

  window.JellyfieldHD.species.striped = {
    id: 'striped',
    /* the BELL's swept envelope (ruling: not the drape — the arms and
       tentacles may fall out of frame or behind the console card), MEASURED
       through the engine's own layout, pose and projection (a numeric sweep
       of this bellShape over t, phi, the breath and the flutter; setupHero's
       scale and anchor for each layout; the presentation
       lean 0.30 +-0.05 and roll +-0.06; the +-0.07 surge; perspective at
       her depth), in units of scale x pxPerWorld, max over 1440x900,
       3840x2160 and 390x844:
       - up 1.086 (the phone: she sits high in the band and the leaned crown
         comes toward the eye; desktop 1.02-1.03) -> 1.10, so the apex stays
         >= 9 px on screen in the phone band at every phase;
       - down 0.759 (4K; 1440 0.735, the phone 0.40);
       - halfWidth 1.045: the radius of the ring at the bell's origin height
         that heroBox projects; 1.0 is the dome's equator and the rest is
         the near flank's perspective, so the box covers the dome's flanks
         by 1-3 px (1440 / phone) at the tightest pose. The perspective bulge
         of the far-from-axis flank (up to 1.29 at 1440) is carried by that
         projection, not by this figure, exactly as for A.
       The old 0.9 / 0.9 understated the dome by ~20 px a side and ~23 px at
       the top at 1440x900@1; the old down 3.6 was the drape and shrank the
       phone bell to a stub. */
    extent: { halfWidth: 1.045, up: 1.10, down: 0.76 },
    /* the apex star's height (contract): the 0.90 apex plus the 0.06 crown
       lift, as A's 0.66 sits over its 0.60 crown — gold just over the dome,
       not buried in it */
    apexY: 0.96,
    breath: 4.5,
    /* linear RGB. body is LOW on purpose: the engine's body light is one
       colour over the whole dome and would wash the stripes grey, so the
       silver-violet glow of the glass is the skin's own (organField, with
       the stripes cut out of it); body only deepens the thick crown. rim is
       the grazing silver-violet halo; body and rim lean violet (R > G), not
       the neutral blue-grey they were, within 2-4% of the old luminance.
       gold only ever lights the lappet edge's hairline, warm enough to read
       as gold LIGHT rather than khaki; organ / filament are the species'
       reference colours (filament = the tentacles' maroon). */
    palette: { body: [0.24, 0.16, 0.44], rim: [0.86, 0.76, 1.30], gold: [1.55, 1.08, 0.42],
               glow: [1.3, 1.15, 0.85], organ: [0.86, 0.48, 1.0], filament: [0.46, 0.06, 0.15] },
    marginJS: marginJS,
    bell: { glsl: BELL_GLSL },
    /* MARGINAL TENTACLES: C. colorata carries 24, three per octant,
       maroon. Three groups of 8 that differ only in phase fill notches 1-3
       of each octant (the notches sit at pi/32 + j x pi/16; notch 0 of each
       octant is left for the rhopalium), so they hang in threes from the
       clefts. 44 nodes keep their curves smooth at 4K. Each GROWS out of
       its cleft: 0.016 at the root (~4 px at 1440, ~11 px at 4K over the
       first third — the engine's (1 - u)^3 taper) thinning to a 0.0025
       thread. The colour is a deep maroon-red because the engine's fixed
       pale core (0.30, 0.34, 0.46) x prof^6 is added on top: the old wine
       (0.34, 0.08, 0.28) came out lilac-pink at the centre, this one comes
       out maroon-rose (~(0.76, 0.40, 0.61)), a third tone distinct from
       the violet stripes and the pale curtains. farD dims the far side's
       through the bell. LENGTH 3.75 is the drape the owner judged: the
       judged frames (50 ms SwiftShader steps, ~2 s in) hung the old 3.2
       stretched to x1.14, 3.55-3.61 deep; on inextensible chains (E-S)
       3.75 hangs 3.58-3.60 deep, bell-local, once settled (6-12 s) at 16,
       33 and 50 ms alike.
       ORAL ARMS, the showpiece: 4 lace curtains frilled with across >=
       LACE_ACROSS — the engine draws such a ribbon as a curled, spiralling
       sheet with ruffled hems, see STRAND_VS. They hang from a SLIM mouth
       (0.20, flaring to ~0.26 by mid-arm: the old 0.46 root was a bright
       sheet of satin parked over the lower bell, in front of the stripes)
       and their hems are FINELY CRIMPED rather than lazily waved: amp 0.028
       at 22 per unit, so the engine's pleat crests (3 per wave) run 66 per
       unit and render at every size (~0.26 rad per pixel at 1440@1, under
       its 0.8 fade). The ruffle's turn per segment is amp x freq^2 x seg,
       independent of resolution: 192 nodes over 3.6 hold it at 0.26 rad
       (128 nodes over 3.4 was 0.37), and 18 columns across cut the curl's
       facets to 0.135 rad (was 0.18), so turns stay round at 4K 1:1. The
       colour is a pale ROSE-SHELL, apart from the violet bell. Measured at
       4K, the arms' brightest rows hang below the hem rather than inside
       the bell, in front of the stripes.
       LENGTH 3.6 is the drape the owner judged. The engine's chains are
       inextensible (heroVerlet, E-S), so an arm's drawn length IS its
       length, at any frame rate and node count. Before that, three
       constraint passes let a 192-node arm stretch with time and frame
       rate, so this file had set 3.0 and the judged frames (50 ms
       SwiftShader steps, ~2 s in) hung it ~3.5 long and 3.4 deep. 3.6
       now hangs 3.44 deep, bell-local, after 6 s at 16, 33 and 50 ms
       alike (3-4 bell lengths, as the spec says). The chains are CPU
       verlet, so nodes are cheap. Widths are FULL widths in bell-local
       units. */
    chains: [
      { kind: 'tentacle', count: 8, nodes: 44, length: 3.75, root: 'margin', phase: 0.2945243,
        width: [0.016, 0.0025], frill: null, color: [0.46, 0.06, 0.15], goldFerrules: false },
      { kind: 'tentacle', count: 8, nodes: 44, length: 3.75, root: 'margin', phase: 0.4908739,
        width: [0.016, 0.0025], frill: null, color: [0.46, 0.06, 0.15], goldFerrules: false },
      { kind: 'tentacle', count: 8, nodes: 44, length: 3.75, root: 'margin', phase: 0.6872234,
        width: [0.016, 0.0025], frill: null, color: [0.46, 0.06, 0.15], goldFerrules: false },
      { kind: 'oralArm', count: 4, nodes: 192, length: 3.6, root: 'oralDisc', phase: 0.7853982,
        width: [0.20, 0.27], frill: { amp: 0.028, freq: 22, across: 18 }, color: [0.94, 0.72, 0.92], goldFerrules: false }
    ],
    motion: { contraction: 0.22, rimFlutter: 0.015, sway: 1 }
  };
})();
