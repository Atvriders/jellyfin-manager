/* Jellyfield — abyssal water column for "THE DEEP".
 * One file, three internal units:
 *   - Jellyfield2D: the shipped Canvas-2D engine, verbatim (only addition: detach()).
 *   - Jellyfield3D: WebGL water column — instanced shader jellyfish in real depth,
 *     fog, light shafts, camera parallax, ray flow field, 3D shockwave pulses.
 *   - selector: owns window.Jellyfield = { mount, pulse, setActivity }, picks the
 *     renderer (reduced-motion / no-WebGL -> 2D) and demotes 3D -> 2D at runtime.
 * A canvas that has ever held a WebGL context can NEVER return a '2d' context, so
 * every renderer swap replaces the canvas element with a fresh clone first.
 * No libraries. GLSL ES 1.00 (works on webgl2 and webgl1 + ANGLE_instanced_arrays).
 */
(function () {
  'use strict';

  if (window.Jellyfield) { return; }

  /* ===================================================================== 2D == */
  /* The shipped Canvas-2D engine, verbatim, minus its outer IIFE + window
   * assignment + duplicate window.Jellyfield guard (the outer IIFE guards).
   * The ONLY code addition is detach() + the return of the API object. */
  function createJellyfield2D() {
  'use strict';


  /* ---------------------------------------------------------------- palette */
  var MEDUSA = { r: 166, g: 92,  b: 199 }; /* #A65CC7 — creatures */
  var BRAND  = { r: 0,   g: 164, b: 220 }; /* #00A4DC — sonar     */
  var LUMEN  = { r: 76,  g: 242, b: 199 }; /* #4CF2C7             */
  var FOAM   = { r: 234, g: 244, b: 255 }; /* #EAF4FF             */

  function mix(a, b, t) {
    return {
      r: Math.round(a.r + (b.r - a.r) * t),
      g: Math.round(a.g + (b.g - a.g) * t),
      b: Math.round(a.b + (b.b - a.b) * t)
    };
  }
  function rgba(c, a) { return 'rgba(' + c.r + ',' + c.g + ',' + c.b + ',' + a + ')'; }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
  function smoothstep(t) { return t * t * (3 - 2 * t); }

  /* ------------------------------------------------------------------ state */
  var canvas = null, ctx = null;
  var W = 0, H = 0, DPR = 1;
  var running = false, rafId = 0, lastT = 0, mounted = false;

  var reduced = false, mql = null, mqlBound = false;

  var activity = 0, activityTarget = 0;

  var mouseX = -1e5, mouseY = -1e5, mouseIn = false, lastMouseT = -1e9, cursorA = 0;

  /* --- jellyfish pool (preallocated, reused) --- */
  var MAX_JELLY = 60;
  var jellies = new Array(MAX_JELLY);
  var jellyCount = 0;

  /* --- ripple slots (max 6 concurrent, reused) --- */
  var MAX_RIPPLE = 6;
  var ripples = new Array(MAX_RIPPLE);
  var i0;
  for (i0 = 0; i0 < MAX_RIPPLE; i0++) {
    ripples[i0] = { active: false, x: 0, y: 0, r: 0, maxR: 0, speed: 0, delay: 0, strength: 0, alpha: 0 };
  }

  /* --- sonar flash --- */
  var flash = { active: false, x: 0, y: 0, t: 0, dur: 0.5 };

  /* --- prerendered sprites (built once; backdrops rebuilt on resize) --- */
  var bellSprites = [];       /* 3 color variants */
  var giantSprite = null;
  var lightSprite = null;     /* cursor light */
  var ringSprite = null;      /* ripple ring, peak at RING_PEAK of half-size */
  var flashSprite = null;
  var RING_PEAK = 0.82;
  var SPR_W = 200, SPR_H = 250, SPR_CX = 100, SPR_CY = 92; /* bell center in sprite */
  var bgCanvas = null, vigCanvas = null;

  /* ------------------------------------------------------------ bell sprite */
  function makeBellSprite(m, soft) {
    /* m: 0..1 medusa->brand mix bias; soft: near-giant variant */
    var c = document.createElement('canvas');
    c.width = SPR_W; c.height = SPR_H;
    var g = c.getContext('2d');
    var core = mix(MEDUSA, BRAND, m * 0.35);
    var rim  = mix(BRAND, MEDUSA, 0.15 + m * 0.2);
    var aMul = soft ? 0.4 : 1;
    var R = 62;

    /* outer aura */
    var aura = g.createRadialGradient(SPR_CX, SPR_CY, 6, SPR_CX, SPR_CY, soft ? 118 : 98);
    aura.addColorStop(0, rgba(core, 0.30 * aMul));
    aura.addColorStop(0.55, rgba(rim, 0.11 * aMul));
    aura.addColorStop(1, rgba(rim, 0));
    g.fillStyle = aura;
    g.fillRect(0, 0, SPR_W, SPR_H);

    /* dome path with scalloped margin (4 lobes) */
    function domePath() {
      g.beginPath();
      g.moveTo(SPR_CX - R, 108);
      g.bezierCurveTo(SPR_CX - R, 34, SPR_CX + R, 34, SPR_CX + R, 108);
      var lobes = 4, x0 = SPR_CX + R, span = 2 * R, k;
      for (k = 1; k <= lobes; k++) {
        var ex = x0 - (span * k) / lobes;
        var cxm = x0 - (span * (k - 0.5)) / lobes;
        g.quadraticCurveTo(cxm, 124, ex, 105);
      }
      g.closePath();
    }

    domePath();
    g.save();
    g.clip();
    /* bell body fill: medusa core -> brand rim */
    var body = g.createRadialGradient(SPR_CX, 80, 4, SPR_CX, 86, R * 1.18);
    body.addColorStop(0, rgba(core, 0.88 * aMul));
    body.addColorStop(0.45, rgba(mix(core, rim, 0.5), 0.5 * aMul));
    body.addColorStop(0.8, rgba(rim, 0.4 * aMul));
    body.addColorStop(1, rgba(rim, 0.06 * aMul));
    g.fillStyle = body;
    g.fillRect(0, 0, SPR_W, SPR_H);
    /* radial canals */
    g.strokeStyle = rgba(FOAM, 0.15 * aMul);
    g.lineWidth = 1.4;
    var canals = 5, ci;
    for (ci = 0; ci < canals; ci++) {
      var fx = ci / (canals - 1) - 0.5;
      g.beginPath();
      g.moveTo(SPR_CX, 56);
      g.quadraticCurveTo(SPR_CX + fx * R * 0.7, 84, SPR_CX + fx * R * 1.55, 106);
      g.stroke();
    }
    /* bright nucleus */
    var nuc = g.createRadialGradient(SPR_CX, 80, 0, SPR_CX, 82, 27);
    nuc.addColorStop(0, rgba(FOAM, 0.85 * aMul));
    nuc.addColorStop(0.4, rgba(mix(core, FOAM, 0.4), 0.4 * aMul));
    nuc.addColorStop(1, rgba(core, 0));
    g.fillStyle = nuc;
    g.fillRect(0, 0, SPR_W, SPR_H);
    /* faint bioluminescent underside at the margin */
    var und = g.createRadialGradient(SPR_CX, 108, 2, SPR_CX, 108, R * 0.9);
    und.addColorStop(0, rgba(LUMEN, 0.14 * aMul));
    und.addColorStop(1, rgba(LUMEN, 0));
    g.fillStyle = und;
    g.fillRect(0, 0, SPR_W, SPR_H);
    g.restore();

    /* rim stroke */
    domePath();
    g.strokeStyle = rgba(mix(rim, FOAM, 0.35), 0.5 * aMul);
    g.lineWidth = 2.2;
    g.stroke();
    /* crisper top-dome highlight */
    g.beginPath();
    g.moveTo(SPR_CX - R * 0.78, 78);
    g.bezierCurveTo(SPR_CX - R * 0.6, 46, SPR_CX + R * 0.6, 46, SPR_CX + R * 0.78, 78);
    g.strokeStyle = rgba(FOAM, 0.28 * aMul);
    g.lineWidth = 1.6;
    g.stroke();
    return c;
  }

  function makeRadialSprite(size, stops) {
    var c = document.createElement('canvas');
    c.width = size; c.height = size;
    var g = c.getContext('2d');
    var grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    var i;
    for (i = 0; i < stops.length; i++) { grad.addColorStop(stops[i][0], stops[i][1]); }
    g.fillStyle = grad;
    g.fillRect(0, 0, size, size);
    return c;
  }

  function buildSprites() {
    bellSprites[0] = makeBellSprite(0.0, false);
    bellSprites[1] = makeBellSprite(0.5, false);
    bellSprites[2] = makeBellSprite(1.0, false);
    giantSprite = makeBellSprite(0.3, true);
    lightSprite = makeRadialSprite(256, [
      [0, rgba(mix(BRAND, FOAM, 0.55), 0.22)],
      [0.45, rgba(mix(BRAND, FOAM, 0.2), 0.09)],
      [1, rgba(BRAND, 0)]
    ]);
    flashSprite = makeRadialSprite(256, [
      [0, rgba(mix(BRAND, FOAM, 0.7), 0.9)],
      [0.35, rgba(BRAND, 0.35)],
      [1, rgba(BRAND, 0)]
    ]);
    /* sonar ring: soft-edged annulus with its peak at RING_PEAK */
    var c = document.createElement('canvas');
    c.width = 512; c.height = 512;
    var g = c.getContext('2d');
    var grad = g.createRadialGradient(256, 256, 0, 256, 256, 256);
    grad.addColorStop(0, rgba(BRAND, 0));
    grad.addColorStop(RING_PEAK - 0.16, rgba(BRAND, 0));
    grad.addColorStop(RING_PEAK - 0.05, rgba(BRAND, 0.28));
    grad.addColorStop(RING_PEAK, rgba(mix(BRAND, FOAM, 0.65), 0.85));
    grad.addColorStop(RING_PEAK + 0.05, rgba(BRAND, 0.25));
    grad.addColorStop(clamp(RING_PEAK + 0.14, 0, 1), rgba(BRAND, 0));
    grad.addColorStop(1, rgba(BRAND, 0));
    g.fillStyle = grad;
    g.fillRect(0, 0, 512, 512);
    ringSprite = c;
  }

  /* -------------------------------------------------- background / vignette */
  function buildBackdrops() {
    /* water column gradient + light shaft (under everything) */
    bgCanvas = document.createElement('canvas');
    bgCanvas.width = Math.max(1, Math.round(W * DPR));
    bgCanvas.height = Math.max(1, Math.round(H * DPR));
    var g = bgCanvas.getContext('2d');
    g.setTransform(DPR, 0, 0, DPR, 0, 0);
    var grad = g.createLinearGradient(0, 0, 0, H);
    grad.addColorStop(0, '#0B2036');
    grad.addColorStop(0.34, '#08182B');
    grad.addColorStop(1, '#040A12');
    g.fillStyle = grad;
    g.fillRect(0, 0, W, H);
    /* faint light shaft falling from the surface */
    var sx = W * 0.6, i;
    for (i = 0; i < 5; i++) {
      var half = W * (0.045 + i * 0.05);
      var sg = g.createLinearGradient(0, 0, 0, H * 0.74);
      sg.addColorStop(0, 'rgba(130,185,235,' + (0.05 - i * 0.009).toFixed(3) + ')');
      sg.addColorStop(1, 'rgba(130,185,235,0)');
      g.fillStyle = sg;
      g.beginPath();
      g.moveTo(sx - half * 0.45, -4);
      g.lineTo(sx + half * 0.45, -4);
      g.lineTo(sx + half * 1.7, H * 0.74);
      g.lineTo(sx - half * 1.7, H * 0.74);
      g.closePath();
      g.fill();
    }

    /* vignette (over everything): darker at edges */
    vigCanvas = document.createElement('canvas');
    vigCanvas.width = bgCanvas.width;
    vigCanvas.height = bgCanvas.height;
    var v = vigCanvas.getContext('2d');
    v.setTransform(DPR, 0, 0, DPR, 0, 0);
    var m = Math.max(W, H);
    var vg = v.createRadialGradient(W * 0.5, H * 0.42, Math.min(W, H) * 0.3, W * 0.5, H * 0.5, m * 0.78);
    vg.addColorStop(0, 'rgba(2,5,10,0)');
    vg.addColorStop(0.7, 'rgba(2,5,10,0.22)');
    vg.addColorStop(1, 'rgba(2,5,10,0.6)');
    v.fillStyle = vg;
    v.fillRect(0, 0, W, H);
  }

  /* -------------------------------------------------------------- jellyfish */
  function makeJelly() {
    return {
      x: 0, y: 0, depth: 0, scale: 1, baseAlpha: 0.6,
      speed: 0, kick: 0,
      phase: 0, phaseRate: 1,
      wobPhase: 0, wobRate: 1, wobAmp: 8,
      vx: 0, vy: 0,               /* transient impulses (ripples) */
      ox: 0, oy: 0,               /* eased flow-field offset */
      tox: 0, toy: 0,             /* flow-field target (scratch) */
      bright: 1, brightT: 1,
      seed: 0, sprite: 0, giant: false,
      tentN: 5, tentLen: 60,
      tentColor: '', armColor: ''
    };
  }
  for (i0 = 0; i0 < MAX_JELLY; i0++) { jellies[i0] = makeJelly(); }

  function initJelly(j, giant) {
    var d = giant ? (0.96 + Math.random() * 0.04) : Math.random();
    j.depth = d;
    j.giant = giant;
    j.scale = giant ? (1.6 + Math.random() * 0.5) : (0.35 + d * 0.75); /* 0.35..1.1 */
    j.baseAlpha = giant ? 0.16 : (0.34 + d * 0.56);
    j.x = Math.random() * W;
    j.y = Math.random() * H;
    j.speed = (7 + Math.random() * 9) * (0.45 + d * 0.75) * (giant ? 0.5 : 1);
    j.kick = (16 + Math.random() * 14) * (0.5 + d * 0.6);
    j.phase = Math.random() * Math.PI * 2;
    j.phaseRate = (0.9 + Math.random() * 0.7) * (giant ? 0.5 : 1);
    j.wobPhase = Math.random() * Math.PI * 2;
    j.wobRate = 0.25 + Math.random() * 0.35;
    j.wobAmp = (5 + Math.random() * 9) * (0.5 + d);
    j.vx = 0; j.vy = 0; j.ox = 0; j.oy = 0; j.bright = 1; j.brightT = 1;
    j.seed = Math.random() * Math.PI * 2;
    j.sprite = (Math.random() * 3) | 0;
    j.tentN = 4 + ((Math.random() * 3) | 0);      /* 4..6 */
    j.tentLen = 68 + Math.random() * 52;
    var tm = Math.random();
    j.tentColor = rgba(mix(MEDUSA, BRAND, tm * 0.5), 0.62);
    j.armColor = rgba(mix(MEDUSA, FOAM, 0.25), 0.42);
  }

  function populate() {
    var mobile = W <= 720;
    var n = mobile ? 25 : 48;
    var giants = mobile ? 1 : 2;
    jellyCount = Math.min(MAX_JELLY, n + giants);
    var i;
    for (i = 0; i < jellyCount; i++) {
      initJelly(jellies[i], i >= jellyCount - giants);
    }
    /* depth sort once: far first, giants (nearest) last; depth never changes */
    jellies.sort(function (a, b) { return a.depth - b.depth; });
  }

  /* ----------------------------------------------------------------- update */
  function update(dt, now) {
    activity += (activityTarget - activity) * Math.min(1, dt * 1.6);
    if (Math.abs(activityTarget - activity) < 0.002) { activity = activityTarget; }

    /* cursor light presence (fade after 2s idle or pointer gone) */
    var wantCursor = mouseIn && (now - lastMouseT < 2000);
    cursorA += ((wantCursor ? 1 : 0) - cursorA) * Math.min(1, dt * 4);

    /* ripples */
    var ri;
    for (ri = 0; ri < MAX_RIPPLE; ri++) {
      var rp = ripples[ri];
      if (!rp.active) { continue; }
      if (rp.delay > 0) { rp.delay -= dt; continue; }
      rp.r += rp.speed * dt;
      var f = 1 - rp.r / rp.maxR;
      if (f <= 0) { rp.active = false; continue; }
      rp.alpha = f * f * Math.min(1, rp.strength);
    }
    if (flash.active) {
      flash.t += dt;
      if (flash.t >= flash.dur) { flash.active = false; }
    }

    var speedMul = 0.55 + activity * 1.5;
    var i;
    for (i = 0; i < jellyCount; i++) {
      var j = jellies[i];
      j.phase += j.phaseRate * (0.65 + activity * 0.9) * dt;
      j.wobPhase += j.wobRate * dt;

      /* bell contraction: sharp on the positive half -> propulsion kick */
      var s = Math.sin(j.phase);
      var c = s > 0 ? s * s : 0;

      /* transient impulses decay */
      var decay = Math.max(0, 1 - dt * 2.4);
      j.vx *= decay; j.vy *= decay;

      j.x += (Math.sin(j.wobPhase) * j.wobAmp * 0.55 + j.vx) * dt;
      j.y += (-(j.speed + j.kick * c) * speedMul + j.vy) * dt;

      /* wrap: rises past the surface -> respawn below */
      var mrg = 170 * j.scale;
      if (j.y < -mrg) { j.y = H + mrg * 0.8; j.x = Math.random() * W; }
      else if (j.y > H + mrg * 1.5) { j.y = -mrg * 0.5; }
      if (j.x < -mrg) { j.x = W + mrg; } else if (j.x > W + mrg) { j.x = -mrg; }

      /* flow field: part around cursor, smoothstep falloff, depth-scaled */
      j.tox = 0; j.toy = 0;
      var bT = 1;
      if (mouseIn) {
        var dx = j.x - mouseX, dy = j.y - mouseY;
        var R = 180 * (0.45 + j.depth * 0.75);
        var d2 = dx * dx + dy * dy;
        if (d2 < R * R && d2 > 0.01) {           /* early-out for far jellies */
          var d = Math.sqrt(d2);
          var t = smoothstep(1 - d / R);
          var push = (62 * (0.4 + j.depth)) * t;
          j.tox = (dx / d) * push;
          j.toy = (dy / d) * push;
          bT = 1 + 0.5 * t;                       /* brighten up to 1.5x */
        }
      }
      j.ox += (j.tox - j.ox) * Math.min(1, dt * 4.2);
      j.oy += (j.toy - j.oy) * Math.min(1, dt * 4.2);

      /* ripple wavefronts push + flash as they pass */
      for (ri = 0; ri < MAX_RIPPLE; ri++) {
        var rp2 = ripples[ri];
        if (!rp2.active || rp2.delay > 0) { continue; }
        var rdx = j.x - rp2.x, rdy = j.y - rp2.y;
        var band = 70;
        var lo = rp2.r - band, hi = rp2.r + band;
        var rd2 = rdx * rdx + rdy * rdy;
        if (rd2 > hi * hi || (lo > 0 && rd2 < lo * lo)) { continue; }
        var rd = Math.sqrt(rd2);
        if (rd < 0.5) { continue; }
        var w = 1 - Math.abs(rd - rp2.r) / band;
        w = w * w * rp2.alpha;
        j.vx += (rdx / rd) * w * 340 * dt * (0.4 + j.depth);
        j.vy += (rdy / rd) * w * 340 * dt * (0.4 + j.depth);
        var rb = 1 + w * 1.1;
        if (rb > bT) { bT = rb; }
      }

      j.brightT = bT;
      j.bright += (j.brightT - j.bright) * Math.min(1, dt * 3);
    }
  }

  /* ------------------------------------------------------------------- draw */
  function drawJelly(j, dim) {
    var s = Math.sin(j.phase);
    var c = s > 0 ? s * s : 0;
    var sqX = 1 - 0.26 * c;   /* bell narrows at contraction */
    var sqY = 1 + 0.17 * c;   /* ...and elongates */
    var sc = j.scale * 0.55;  /* sprite unit -> css px */
    var alpha = j.baseAlpha * (0.8 + 0.3 * activity) * Math.min(1.35, j.bright) * dim;
    if (alpha <= 0.01) { return; }

    ctx.save();
    ctx.translate(j.x + j.ox, j.y + j.oy);

    /* tentacles behind the bell */
    var n = j.tentN, i;
    ctx.lineCap = 'round';
    ctx.strokeStyle = j.tentColor;
    ctx.lineWidth = Math.max(0.6, 1.7 * sc);
    ctx.globalAlpha = alpha * 0.8;
    var lean = -Math.sin(j.wobPhase) * 9 * sc;
    for (i = 0; i < n; i++) {
      var fx = n > 1 ? (i / (n - 1) - 0.5) : 0;
      var ax = fx * 96 * sc * sqX;
      var ay = 15 * sc * sqY;
      var len = j.tentLen * sc * (1 - 0.16 * c) * (1 - Math.abs(fx) * 0.5);
      var sw1 = Math.sin(j.phase * 0.85 + j.seed + i * 1.9) * 15 * sc;
      var sw2 = Math.sin(j.phase * 0.85 + j.seed + i * 1.9 + 1.45) * 24 * sc;
      ctx.beginPath();
      ctx.moveTo(ax, ay);
      ctx.quadraticCurveTo(ax + sw1 * 0.55 + lean * 0.3, ay + len * 0.5, ax + sw1 + lean * 0.6, ay + len * 0.82);
      ctx.quadraticCurveTo(ax + sw1 + (sw2 - sw1) * 0.7 + lean * 0.8, ay + len * 0.95, ax + sw2 + lean, ay + len);
      ctx.stroke();
    }
    /* two thicker oral arms */
    ctx.strokeStyle = j.armColor;
    ctx.lineWidth = Math.max(1, 3.1 * sc);
    ctx.globalAlpha = alpha * 0.6;
    for (i = 0; i < 2; i++) {
      var side = i === 0 ? -1 : 1;
      var ax2 = side * 14 * sc * sqX;
      var len2 = j.tentLen * sc * 0.62 * (1 - 0.16 * c);
      var sw = Math.sin(j.phase * 0.85 + j.seed + 3 + i * 2.4) * 12 * sc;
      ctx.beginPath();
      ctx.moveTo(ax2, 12 * sc * sqY);
      ctx.quadraticCurveTo(ax2 + sw * 0.6 + lean * 0.4, len2 * 0.55, ax2 + sw + lean * 0.8, len2);
      ctx.stroke();
    }

    /* bell */
    var spr = j.giant ? giantSprite : bellSprites[j.sprite];
    var bw = SPR_W * sc * sqX;
    var bh = SPR_H * sc * sqY;
    ctx.globalAlpha = alpha;
    ctx.drawImage(spr, -SPR_CX * sc * sqX, -SPR_CY * sc * sqY, bw, bh);
    /* excitement glow pass */
    if (j.bright > 1.06) {
      ctx.globalAlpha = Math.min(0.7, (j.bright - 1) * 0.75) * dim;
      ctx.drawImage(spr, -SPR_CX * sc * sqX * 1.06, -SPR_CY * sc * sqY * 1.06, bw * 1.06, bh * 1.06);
    }
    ctx.restore();
  }

  function draw() {
    /* water */
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    ctx.drawImage(bgCanvas, 0, 0, W, H);

    /* luminous passes */
    ctx.globalCompositeOperation = 'lighter';

    /* cursor light illuminates the water locally */
    if (cursorA > 0.01) {
      var lr = 300 + activity * 90;
      ctx.globalAlpha = cursorA * 0.9;
      ctx.drawImage(lightSprite, mouseX - lr, mouseY - lr, lr * 2, lr * 2);
    }

    /* jellyfish, far -> near (pre-sorted by depth); dimmed under the text column */
    var i;
    for (i = 0; i < jellyCount; i++) {
      var jd = jellies[i];
      drawJelly(jd, columnDim(jd.x + jd.ox));
    }

    /* ripples */
    for (i = 0; i < MAX_RIPPLE; i++) {
      var rp = ripples[i];
      if (!rp.active || rp.delay > 0 || rp.r <= 1) { continue; }
      var dsz = (rp.r / RING_PEAK) * 2;
      ctx.globalAlpha = rp.alpha * 0.9;
      ctx.drawImage(ringSprite, rp.x - dsz / 2, rp.y - dsz / 2, dsz, dsz);
    }

    /* sonar flash at the pulse origin */
    if (flash.active) {
      var ft = 1 - flash.t / flash.dur;
      var fr = 120 + (1 - ft) * 160;
      ctx.globalAlpha = ft * ft * 0.85;
      ctx.drawImage(flashSprite, flash.x - fr, flash.y - fr, fr * 2, fr * 2);
    }

    /* field-energy shimmer: whole water brightens with activity */
    if (activity > 0.02) {
      ctx.globalAlpha = activity * 0.06;
      ctx.fillStyle = '#7FD8FF';
      ctx.fillRect(0, 0, W, H);
    }

    /* reset compositing, then vignette on top */
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    ctx.drawImage(vigCanvas, 0, 0, W, H);
  }

  /* --------------------------------------------------------- reduced motion */
  function renderStatic() {
    if (!ctx) { return; }
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    ctx.drawImage(bgCanvas, 0, 0, W, H);
    ctx.globalCompositeOperation = 'lighter';
    var n = Math.min(8, jellyCount), i;
    for (i = 0; i < n; i++) {
      var j = jellies[Math.min(jellyCount - 1, Math.floor((i + 0.5) * jellyCount / n))];
      /* deterministic calm placement, dim, near-still */
      j.x = W * (0.1 + 0.8 * ((i * 0.618034 + 0.07) % 1));
      j.y = H * (0.12 + 0.74 * ((i * 0.381966 + 0.23) % 1));
      j.ox = 0; j.oy = 0; j.bright = 1; j.phase = 2.2 + i * 0.9;
      drawJelly(j, 0.55 * columnDim(j.x));
    }
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    ctx.drawImage(vigCanvas, 0, 0, W, H);
  }

  /* ------------------------------------------------------------------- loop */
  function frame(t) {
    rafId = 0;
    if (!running) { return; }
    var dt = (t - lastT) / 1000;
    lastT = t;
    if (dt > 0.05) { dt = 0.05; }
    if (dt > 0) { update(dt, t); }
    draw();
    rafId = requestAnimationFrame(frame);
  }

  function start() {
    if (running || reduced || !mounted) { return; }
    running = true;
    lastT = performance.now();
    if (!rafId) { rafId = requestAnimationFrame(frame); }
  }

  function stop() {
    running = false;
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
  }

  /* ----------------------------------------------------------- mgmt/events */

  /* Text-column dim band (a11y): the page's overlay text (tick labels,
     eyebrows) sits directly on the water. Jellies passing under it are dimmed
     toward DIM_FLOOR so small benthos text keeps a dark backdrop. */
  var dimL = 0, dimR = -1, DIM_FEATHER = 120, DIM_FLOOR = 0.35;
  function measureColumn() {
    var col = document.querySelector('.column');
    if (col) {
      var r = col.getBoundingClientRect();
      dimL = r.left - 60;   /* also cover rail marks hanging left of the column */
      dimR = r.right;
    } else {
      dimL = 0; dimR = -1;  /* disabled */
    }
  }
  function columnDim(x) {
    if (dimR < dimL) { return 1; }
    var t;
    if (x >= dimL && x <= dimR) { t = 0; }
    else if (x < dimL) { t = (dimL - x) / DIM_FEATHER; }
    else { t = (x - dimR) / DIM_FEATHER; }
    if (t >= 1) { return 1; }
    return DIM_FLOOR + (1 - DIM_FLOOR) * smoothstep(t);
  }

  function applyResize() {
    if (!canvas) { return; }
    var rect = canvas.getBoundingClientRect();
    var newW = rect.width || window.innerWidth;
    var newH = rect.height || window.innerHeight;
    var newDPR = Math.min(2, window.devicePixelRatio || 1);
    measureColumn();
    if (newW === W && newH === H && newDPR === DPR && bgCanvas) { return; }
    var oldW = W, oldH = H;
    W = newW; H = newH; DPR = newDPR;
    canvas.width = Math.max(1, Math.round(W * DPR));
    canvas.height = Math.max(1, Math.round(H * DPR));
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    buildBackdrops();
    var wasMobile = oldW > 0 && oldW <= 720;
    var isMobile = W <= 720;
    if (jellyCount === 0 || oldW <= 0 || oldH <= 0 || wasMobile !== isMobile) {
      populate();               /* first mount, or the density breakpoint flipped */
    } else {
      /* rescale existing positions so the field survives the resize
         instead of teleporting to fresh random spots mid-gesture */
      var sx = W / oldW, sy = H / oldH, i;
      for (i = 0; i < jellyCount; i++) { jellies[i].x *= sx; jellies[i].y *= sy; }
    }
    if (reduced) { renderStatic(); }
  }

  /* Debounced 150ms trailing: drag-resize and mobile URL-bar/soft-keyboard
     viewport changes fire resize per frame, and each applyResize reallocates
     two full-screen DPR-scaled canvases — run it once per settled size. */
  var resizeTimer = 0;
  function resize() {
    if (resizeTimer) { clearTimeout(resizeTimer); }
    resizeTimer = setTimeout(function () { resizeTimer = 0; applyResize(); }, 150);
  }

  function onMouseMove(e) {
    if (reduced) { return; }
    mouseX = e.clientX;
    mouseY = e.clientY;
    mouseIn = true;
    lastMouseT = performance.now();
  }

  function onMouseOut(e) {
    if (!e.relatedTarget) { mouseIn = false; }
  }

  function onVisibility() {
    if (document.hidden) { stop(); }
    else if (!reduced) { start(); }
  }

  function onMotionChange() {
    reduced = !!(mql && mql.matches);
    if (reduced) {
      stop();
      activity = 0; activityTarget = 0; cursorA = 0; mouseIn = false;
      var i;
      for (i = 0; i < MAX_RIPPLE; i++) { ripples[i].active = false; }
      flash.active = false;
      renderStatic();
    } else {
      populate();
      start();
    }
  }

  /* --------------------------------------------------------------- API ---- */
  function mount(canvasEl) {
    if (!canvasEl || typeof canvasEl.getContext !== 'function') { return; }
    if (mounted && canvasEl === canvas) { return; }
    if (mounted) { stop(); }
    canvas = canvasEl;
    ctx = canvas.getContext('2d');
    if (!ctx) { return; }
    if (!bellSprites[0]) { buildSprites(); }

    mql = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    reduced = !!(mql && mql.matches);
    if (mql && !mqlBound) {
      mqlBound = true;
      if (typeof mql.addEventListener === 'function') { mql.addEventListener('change', onMotionChange); }
      else if (typeof mql.addListener === 'function') { mql.addListener(onMotionChange); }
    }

    if (!mounted) {
      window.addEventListener('mousemove', onMouseMove, { passive: true });
      window.addEventListener('mouseout', onMouseOut, { passive: true });
      /* NOTE: no engine-internal click listener — the page owns click->pulse
         wiring (it excludes controls that fire their own bloom), so each click
         spawns exactly one ripple. */
      window.addEventListener('resize', resize);
      document.addEventListener('visibilitychange', onVisibility);
    }
    mounted = true;

    applyResize();
    if (reduced) { renderStatic(); }
    else { start(); }
  }

  function pulse(clientX, clientY, strength) {
    if (!mounted || reduced) { return; }
    var s = (typeof strength === 'number' && isFinite(strength)) ? strength : 1;
    var x = typeof clientX === 'number' ? clientX : W * 0.5;
    var y = typeof clientY === 'number' ? clientY : H * 0.5;
    var rings = s >= 2 ? 3 : 1;
    var k;
    for (k = 0; k < rings; k++) {
      /* claim a free slot, else recycle the most-expired ripple (cap 6) */
      var slot = null, oldest = null, oldestF = -1, ri;
      for (ri = 0; ri < MAX_RIPPLE; ri++) {
        var rp = ripples[ri];
        if (!rp.active) { slot = rp; break; }
        var fexp = rp.maxR > 0 ? rp.r / rp.maxR : 1;
        if (fexp > oldestF) { oldestF = fexp; oldest = rp; }
      }
      if (!slot) { slot = oldest; }
      slot.active = true;
      slot.x = x;
      slot.y = y;
      slot.r = 0;
      slot.maxR = (rings > 1 ? Math.max(W, H) * 0.85 : 430) * (0.75 + Math.min(s, 3) * 0.18);
      slot.speed = (rings > 1 ? 560 : 430) * (0.8 + Math.min(s, 3) * 0.12);
      slot.delay = k * 0.18;                       /* staggered ~180ms */
      slot.strength = Math.min(s, 3) * (1 - k * 0.22);
      slot.alpha = 0;
    }
    if (s >= 2) {
      flash.active = true;
      flash.x = x;
      flash.y = y;
      flash.t = 0;
    }
  }

  function setActivity(level) {
    var v = (typeof level === 'number' && isFinite(level)) ? level : 0;
    activityTarget = clamp(v, 0, 1);
  }

  /* detach(): the ONLY addition to the shipped 2D engine — removes its
     window/document listeners so the selector can swap renderers cleanly. */
  function detach() {
    stop();
    if (resizeTimer) { clearTimeout(resizeTimer); resizeTimer = 0; }
    if (mounted) {
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseout', onMouseOut);
      window.removeEventListener('resize', resize);
      document.removeEventListener('visibilitychange', onVisibility);
    }
    if (mql && mqlBound) {
      mqlBound = false;
      if (typeof mql.removeEventListener === 'function') { mql.removeEventListener('change', onMotionChange); }
      else if (typeof mql.removeListener === 'function') { mql.removeListener(onMotionChange); }
      mql = null;
    }
    mounted = false;
    canvas = null;
    ctx = null;
    /* Bust the cached sizing so a remount on a swapped-in fresh canvas
       re-runs the full applyResize (canvas.width/height + setTransform DPR)
       instead of early-returning on unchanged W/H/DPR — otherwise the new
       identity-transform context draws quarter-scale on DPR>1 displays. */
    W = 0; H = 0;
    bgCanvas = null;
  }

  return { mount: mount, pulse: pulse, setActivity: setActivity, detach: detach };
  }

  /* ===================================================================== 3D == */
  function createJellyfield3D() {
    'use strict';

    /* ------------------------------------------------------------ constants */
    var MAX_JELLY = 60;
    var MAX_SHELL = 6;
    var FOVY = 55 * Math.PI / 180;
    var NEARZ = 0.5, FARZ = 80;
    var FOG_D = 0.048;               /* exponential fog toward the abyss */
    var MAXTILT = 3 * Math.PI / 180; /* mouse parallax: +-3 degrees */
    var PULSE_Z = -13;               /* shockwaves detonate at mid-depth */
    var CURSOR_Z = -10;              /* cursor light lives at mid-depth */
    var BELL_SEG = 24, BELL_RINGS = 14;
    var TENT_RIBS = 6, TENT_SEG = 14;

    /* --------------------------------------------------------------- state */
    var canvas = null, gl = null, isGL2 = false, instExt = null;
    var W = 0, H = 0, DPR = 1, aspect = 1;
    var tanY = Math.tan(FOVY / 2), tanXA = tanY;
    var running = false, rafId = 0, lastT = 0, mounted = false, dead = false;
    var contextLost = false;
    var timeS = 0;

    var activity = 0, activityTarget = 0;

    var mouseNX = 0, mouseNY = 0, mouseIn = false, lastMouseT = -1e9, cursorA = 0;

    /* camera */
    var eyeX = 0, eyeY = 0, eyeZ = 0;
    var yaw = 0, pitch = 0;
    var rgt = new Float32Array(3), upv = new Float32Array(3), bck = new Float32Array(3);
    var rayDX = 0, rayDY = 0, rayDZ = -1;   /* pointer ray (world) */
    var urx = 0, ury = 0, urz = -1;          /* scratch unproject ray */

    var mProj = new Float32Array(16), mView = new Float32Array(16), mVP = new Float32Array(16);

    /* a11y text-column dim band (CSS px, fed to the shaders) */
    var dimL = 0, dimR = -1;

    /* GL objects (rebuilt wholesale on context restore) */
    var progBack = null, progBell = null, progTent = null, progShell = null, progGlow = null;
    var quadBuf = null, bellVBuf = null, bellIBuf = null, tentVBuf = null, tentIBuf = null;
    var instBuf = null, shellBuf = null;
    var bellIdxCount = 0, tentIdxCount = 0;

    /* preallocated per-frame data (no allocation in the hot loop) */
    var instF = new Float32Array(MAX_JELLY * 8);
    var instView = instF.subarray(0, 0);
    var shellF = new Float32Array(MAX_SHELL * 8);
    var shellCount = 0;

    /* ------------------------------------------------------------- jellies */
    function makeJelly() {
      return {
        x: 0, y: 0, z: -20, scale: 1, glow: 0.8, colorMix: 0,
        speed: 0, kick: 0,
        phase: 0, phaseRate: 1,
        wobPhase: 0, wobRate: 1, wobAmp: 0.5,
        vx: 0, vy: 0, vz: 0,
        ox: 0, oy: 0, oz: 0,
        tox: 0, toy: 0, toz: 0,
        bright: 1, brightT: 1,
        seed: 0, near: false
      };
    }
    var jellies = new Array(MAX_JELLY);
    var jellyCount = 0;
    var ji;
    for (ji = 0; ji < MAX_JELLY; ji++) { jellies[ji] = makeJelly(); }

    /* shockwave shells (pulse), max 6 concurrent, slots reused */
    var shells = new Array(MAX_SHELL);
    for (ji = 0; ji < MAX_SHELL; ji++) {
      shells[ji] = { active: false, x: 0, y: 0, z: 0, r: 0, maxR: 0, speed: 0, delay: 0, strength: 0, alpha: 0 };
    }
    var flash = { active: false, x: 0, y: 0, z: 0, t: 0, dur: 0.5 };

    var api = { mount: mount, pulse: pulse, setActivity: setActivity, detach: detach, onFatal: null };

    /* -------------------------------------------------------------- shaders */
    var BACK_VS = [
      'attribute vec2 a_p;',
      'varying vec2 v_uv;',
      'void main() {',
      '  gl_Position = vec4(a_p, 0.0, 1.0);',
      '  v_uv = a_p * 0.5 + 0.5;',
      '}'
    ].join('\n');

    var BACK_FS = [
      'precision mediump float;',
      'varying vec2 v_uv;',
      /* Shaft phases are precomputed sin() values uploaded per frame: an
         unbounded u_time in a mediump (possibly fp16) fragment shader
         quantizes within hours and overflows fp16 max (65504) -> NaN. */
      'uniform vec4 u_shaft;',
      'uniform float u_activity;',
      'uniform float u_aspect;',
      'float shaft(vec2 uv, float off, float wBase, float slope, float sv) {',
      '  float depth = 1.0 - uv.y;',
      '  float cx = (0.62 + off) * u_aspect + depth * slope;',
      '  float w = wBase * (1.0 + depth * 2.4);',
      '  float d = abs(uv.x * u_aspect - cx) / w;',
      '  float fall = max(0.0, 1.0 - d);',
      '  fall *= fall;',
      '  float fade = smoothstep(0.12, 0.9, uv.y);',
      '  return fall * fade * (0.72 + 0.28 * sv);',
      '}',
      'void main() {',
      '  vec3 bot = vec3(0.016, 0.039, 0.071);',   /* #040A12 */
      '  vec3 mid = vec3(0.031, 0.094, 0.169);',   /* #08182B */
      '  vec3 top = vec3(0.043, 0.125, 0.212);',   /* #0B2036 */
      '  vec3 col = mix(bot, mid, smoothstep(0.0, 0.62, v_uv.y));',
      '  col = mix(col, top, smoothstep(0.62, 1.0, v_uv.y));',
      '  float s = 0.0;',
      '  s += shaft(v_uv, 0.0, 0.05, 0.30, u_shaft.x) * 0.085;',
      '  s += shaft(v_uv, -0.10, 0.085, 0.44, u_shaft.y) * 0.062;',
      '  s += shaft(v_uv, 0.09, 0.12, 0.20, u_shaft.z) * 0.050;',
      '  s += shaft(v_uv, -0.23, 0.16, 0.58, u_shaft.w) * 0.036;',
      '  col += vec3(0.51, 0.725, 0.922) * s;',
      '  col += vec3(0.5, 0.85, 1.0) * (u_activity * 0.06);',
      /* radial falloff so the corners breathe dark, like the 2D backdrop */
      '  vec2 cc = vec2((v_uv.x - 0.5) * u_aspect, v_uv.y - 0.45);',
      '  col *= 1.0 - 0.35 * smoothstep(0.35, 1.15, length(cc));',
      '  gl_FragColor = vec4(col, 1.0);',
      '}'
    ].join('\n');

    var DIM_GLSL = [
      'float dimBand(float px) {',
      '  if (u_dim.y < u_dim.x) { return 1.0; }',
      '  float t = 0.0;',
      '  if (px < u_dim.x) { t = (u_dim.x - px) / 120.0; }',
      '  else if (px > u_dim.y) { t = (px - u_dim.y) / 120.0; }',
      '  t = clamp(t, 0.0, 1.0);',
      '  t = t * t * (3.0 - 2.0 * t);',
      '  return 0.35 + 0.65 * t;',
      '}'
    ].join('\n');

    var BELL_VS = [
      'attribute vec3 a_pos;',
      'attribute vec3 a_norm;',
      'attribute vec4 a_i0;',   /* x, y, z, scale */
      'attribute vec4 a_i1;',   /* phase, colorMix, glow, seed */
      'uniform mat4 u_vp;',
      'uniform vec3 u_cam;',
      'uniform vec2 u_dim;',
      'uniform float u_vpw;',
      'uniform float u_time;',
      'uniform float u_activity;',
      'uniform float u_fogD;',
      'varying vec3 v_n;',
      'varying vec3 v_v;',
      'varying vec3 v_col;',
      'varying float v_glow;',
      'varying vec3 v_mp;',
      DIM_GLSL,
      'void main() {',
      '  float phase = a_i1.x;',
      '  float seed = a_i1.w;',
      '  float sp = sin(phase);',
      '  float ctr = max(sp, 0.0);',
      '  ctr = ctr * ctr;',
      '  vec3 p = a_pos;',
      '  p.x *= (1.0 - 0.26 * ctr);',
      '  p.z *= (1.0 - 0.26 * ctr);',
      '  p.y *= (1.0 + 0.17 * ctr);',
      '  vec3 n = a_norm;',
      /* per-instance fixed tilt (bells lean toward/away from camera) + swim lean */
      '  float tx = (fract(seed * 1.131) - 0.5) * 0.9;',
      '  float tz = (fract(seed * 2.417) - 0.5) * 0.9 + sin(u_time * 0.6 + seed) * 0.16;',
      '  float cx = cos(tx); float sx = sin(tx);',
      '  float cz = cos(tz); float sz = sin(tz);',
      '  p = vec3(p.x, p.y * cx - p.z * sx, p.y * sx + p.z * cx);',
      '  n = vec3(n.x, n.y * cx - n.z * sx, n.y * sx + n.z * cx);',
      '  p = vec3(p.x * cz - p.y * sz, p.x * sz + p.y * cz, p.z);',
      '  n = vec3(n.x * cz - n.y * sz, n.x * sz + n.y * cz, n.z);',
      '  vec3 world = a_i0.xyz + p * a_i0.w;',
      '  gl_Position = u_vp * vec4(world, 1.0);',
      '  vec4 cc = u_vp * vec4(a_i0.xyz, 1.0);',
      '  float px = (cc.x / max(cc.w, 0.0001) * 0.5 + 0.5) * u_vpw;',
      '  float dist = length(a_i0.xyz - u_cam);',
      '  float fog = exp(-dist * u_fogD);',
      '  float dm = clamp((dist - 5.0) / 28.0, 0.0, 1.0);',
      '  float cm = clamp(a_i1.y * 0.55 + dm * 0.4, 0.0, 1.0);',
      '  v_col = mix(vec3(0.72, 0.36, 0.92), vec3(0.0, 0.66, 0.9), cm);',
      '  v_glow = a_i1.z * dimBand(px) * fog * (0.8 + 0.3 * u_activity);',
      '  v_n = n;',
      '  v_v = world - u_cam;',
      '  v_mp = a_pos;',
      '}'
    ].join('\n');

    var BELL_FS = [
      'precision mediump float;',
      'varying vec3 v_n;',
      'varying vec3 v_v;',
      'varying vec3 v_col;',
      'varying float v_glow;',
      'varying vec3 v_mp;',
      'void main() {',
      '  vec3 n = normalize(v_n);',
      '  vec3 v = normalize(v_v);',
      '  float ndv = abs(dot(n, v));',
      '  float fres = pow(1.0 - ndv, 1.8);',
      /* translucent body + fresnel rim glow */
      '  vec3 c = v_col * (0.24 + 1.05 * fres);',
      /* emissive nucleus near the bell apex */
      '  float nd = length(v_mp - vec3(0.0, 0.40, 0.0));',
      '  c += mix(v_col, vec3(0.918, 0.957, 1.0), 0.75) * exp(-nd * nd * 3.6) * 0.85;',
      /* bioluminescent underside margin */
      '  float um = smoothstep(0.5, 0.0, v_mp.y);',
      '  c += vec3(0.298, 0.949, 0.780) * um * 0.16;',
      '  gl_FragColor = vec4(c * v_glow, 1.0);',
      '}'
    ].join('\n');

    var TENT_VS = [
      'attribute vec3 a_trs;',  /* t along ribbon, side (-1/1), rib index */
      'attribute vec4 a_i0;',
      'attribute vec4 a_i1;',
      'uniform mat4 u_vp;',
      'uniform vec3 u_cam;',
      'uniform vec3 u_right;',
      'uniform vec2 u_dim;',
      'uniform float u_vpw;',
      'uniform float u_activity;',
      'uniform float u_fogD;',
      'varying float v_a;',
      'varying vec3 v_col;',
      DIM_GLSL,
      'void main() {',
      '  float t = a_trs.x;',
      '  float side = a_trs.y;',
      '  float rib = a_trs.z;',
      '  float phase = a_i1.x;',
      '  float seed = a_i1.w;',
      '  float scale = a_i0.w;',
      '  float sp = sin(phase);',
      '  float ctr = max(sp, 0.0);',
      '  ctr = ctr * ctr;',
      '  float ang = rib * 1.0471976 + seed;',
      '  float lenR = fract(sin(seed * 37.7 + rib * 13.1) * 437.585);',
      '  float len = (1.5 + 1.3 * lenR) * scale * (1.0 - 0.16 * ctr);',
      '  vec3 root = vec3(cos(ang) * 0.5, -0.02, sin(ang) * 0.5) * scale * (1.0 - 0.26 * ctr);',
      '  float sw1 = sin(phase * 0.85 + seed + rib * 1.9 + t * 4.4);',
      '  float sw2 = sin(phase * 0.62 + seed * 1.7 + rib * 1.9 + 1.45 + t * 2.9);',
      '  float bow = sin(seed * 5.3 + rib * 2.7);',
      '  float curve = t * (0.35 + 0.65 * t);',
      '  float sway = ((sw1 * 0.6 + sw2 * 0.4) * 0.62 + bow * 0.3) * scale * curve;',
      '  vec3 p = root + vec3(cos(ang + 1.5708) * sway + cos(ang) * 0.26 * scale * curve,',
      '                       -t * len,',
      '                       sin(ang + 1.5708) * sway + sin(ang) * 0.26 * scale * curve);',
      '  p += u_right * (side * 0.028 * scale * (1.0 - 0.72 * t));',
      '  vec3 world = a_i0.xyz + p;',
      '  gl_Position = u_vp * vec4(world, 1.0);',
      '  vec4 cc = u_vp * vec4(a_i0.xyz, 1.0);',
      '  float px = (cc.x / max(cc.w, 0.0001) * 0.5 + 0.5) * u_vpw;',
      '  float dist = length(a_i0.xyz - u_cam);',
      '  float fog = exp(-dist * u_fogD);',
      '  float cm = clamp(a_i1.y * 0.5, 0.0, 1.0);',
      '  v_col = mix(vec3(0.72, 0.36, 0.92), vec3(0.0, 0.66, 0.9), cm);',
      '  float fade = (1.0 - t) * (1.0 - t);',
      '  v_a = a_i1.z * dimBand(px) * fog * fade * 0.85 * (0.8 + 0.3 * u_activity);',
      '}'
    ].join('\n');

    var TENT_FS = [
      'precision mediump float;',
      'varying float v_a;',
      'varying vec3 v_col;',
      'void main() {',
      '  gl_FragColor = vec4(v_col * v_a, 1.0);',
      '}'
    ].join('\n');

    var SHELL_VS = [
      'attribute vec2 a_q;',
      'attribute vec4 a_i0;',   /* x, y, z, radius */
      'attribute vec4 a_i1;',   /* alpha, -, -, - */
      'uniform mat4 u_vp;',
      'uniform vec3 u_right;',
      'uniform vec3 u_up;',
      'varying vec2 v_q;',
      'varying float v_a;',
      'void main() {',
      '  vec3 world = a_i0.xyz + (u_right * a_q.x + u_up * a_q.y) * a_i0.w;',
      '  gl_Position = u_vp * vec4(world, 1.0);',
      '  v_q = a_q;',
      '  v_a = a_i1.x;',
      '}'
    ].join('\n');

    var SHELL_FS = [
      'precision mediump float;',
      'varying vec2 v_q;',
      'varying float v_a;',
      /* u_dimD: text-column dim band in DEVICE pixels (x=left, y=right,
         z=feather). Shell quads can span the whole screen, so the band is
         evaluated per-fragment via gl_FragCoord — a vertex-interpolated
         factor would miss the band between the quad corners. Keeps peak
         shell glow from washing out bare rail labels (SURFACE/CONSOLE). */
      'uniform vec3 u_dimD;',
      'float dimBand(float px) {',
      '  if (u_dimD.y < u_dimD.x) { return 1.0; }',
      '  float t = 0.0;',
      '  if (px < u_dimD.x) { t = (u_dimD.x - px) / u_dimD.z; }',
      '  else if (px > u_dimD.y) { t = (px - u_dimD.y) / u_dimD.z; }',
      '  t = clamp(t, 0.0, 1.0);',
      '  t = t * t * (3.0 - 2.0 * t);',
      '  return 0.35 + 0.65 * t;',
      '}',
      'void main() {',
      '  float r = length(v_q);',
      '  float band = max(0.0, 1.0 - abs(r - 0.82) / 0.14);',
      '  band *= band;',
      '  float dim = dimBand(gl_FragCoord.x);',
      /* under the dim band, also pull the near-white core back toward the
         base cyan so the ring never approaches label-swallowing white */
      '  vec3 col = mix(vec3(0.0, 0.643, 0.863), vec3(0.918, 0.957, 1.0), band * 0.6 * dim);',
      '  gl_FragColor = vec4(col * band * v_a * dim, 1.0);',
      '}'
    ].join('\n');

    var GLOW_VS = [
      'attribute vec2 a_q;',
      'uniform mat4 u_vp;',
      'uniform vec3 u_pos;',
      'uniform vec3 u_right;',
      'uniform vec3 u_up;',
      'uniform float u_size;',
      'varying vec2 v_q;',
      'void main() {',
      '  vec3 world = u_pos + (u_right * a_q.x + u_up * a_q.y) * u_size;',
      '  gl_Position = u_vp * vec4(world, 1.0);',
      '  v_q = a_q;',
      '}'
    ].join('\n');

    var GLOW_FS = [
      'precision mediump float;',
      'varying vec2 v_q;',
      'uniform vec3 u_color;',
      'uniform float u_int;',
      'void main() {',
      '  float r = length(v_q);',
      '  float a = exp(-r * r * 4.0) * smoothstep(1.0, 0.65, r) * u_int;',
      '  gl_FragColor = vec4(u_color * a, 1.0);',
      '}'
    ].join('\n');

    /* ----------------------------------------------------------- GL helpers */
    function compileShader(type, src) {
      var s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS) && !gl.isContextLost()) {
        gl.deleteShader(s);
        return null;
      }
      return s;
    }

    function makeProg(vsSrc, fsSrc, attrs, uniforms) {
      var vs = compileShader(gl.VERTEX_SHADER, vsSrc);
      if (!vs) { return null; }
      var fs = compileShader(gl.FRAGMENT_SHADER, fsSrc);
      if (!fs) { gl.deleteShader(vs); return null; }
      var p = gl.createProgram();
      var name;
      for (name in attrs) {
        if (Object.prototype.hasOwnProperty.call(attrs, name)) {
          gl.bindAttribLocation(p, attrs[name], name);
        }
      }
      gl.attachShader(p, vs);
      gl.attachShader(p, fs);
      gl.linkProgram(p);
      gl.deleteShader(vs);
      gl.deleteShader(fs);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS) && !gl.isContextLost()) {
        gl.deleteProgram(p);
        return null;
      }
      var obj = { p: p };
      var i;
      for (i = 0; i < uniforms.length; i++) {
        obj[uniforms[i]] = gl.getUniformLocation(p, uniforms[i]);
      }
      return obj;
    }

    function setDiv(loc, d) {
      if (isGL2) { gl.vertexAttribDivisor(loc, d); }
      else { instExt.vertexAttribDivisorANGLE(loc, d); }
    }

    function drawElemInst(count, prim) {
      if (isGL2) { gl.drawElementsInstanced(gl.TRIANGLES, count, gl.UNSIGNED_SHORT, 0, prim); }
      else { instExt.drawElementsInstancedANGLE(gl.TRIANGLES, count, gl.UNSIGNED_SHORT, 0, prim); }
    }

    function drawArrInst(count, prim) {
      if (isGL2) { gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, count, prim); }
      else { instExt.drawArraysInstancedANGLE(gl.TRIANGLE_STRIP, 0, count, prim); }
    }

    /* --------------------------------------------------------------- meshes */
    function buildBellMesh() {
      var cols = BELL_SEG + 1;
      var rows = BELL_RINGS + 1;
      var v = new Float32Array(rows * cols * 6);
      var o = 0, r, s;
      for (r = 0; r < rows; r++) {
        var t = r / BELL_RINGS;
        var th = t * 1.92;                       /* pole to just past the equator */
        var st = Math.sin(th), ct = Math.cos(th);
        for (s = 0; s <= BELL_SEG; s++) {
          var ph = (s / BELL_SEG) * Math.PI * 2;
          var scallop = t > 0.85 ? ((t - 0.85) / 0.15) * 0.07 * Math.sin(4 * ph) : 0;
          var rad = st * (1 + scallop);
          v[o++] = rad * Math.cos(ph);
          v[o++] = ct * 0.72;                    /* bell is wider than tall */
          v[o++] = rad * Math.sin(ph);
          v[o++] = st * Math.cos(ph);
          v[o++] = ct;
          v[o++] = st * Math.sin(ph);
        }
      }
      var idx = new Uint16Array(BELL_RINGS * BELL_SEG * 6);
      o = 0;
      for (r = 0; r < BELL_RINGS; r++) {
        for (s = 0; s < BELL_SEG; s++) {
          var a = r * cols + s, b = a + 1, c = a + cols, d = c + 1;
          idx[o++] = a; idx[o++] = c; idx[o++] = b;
          idx[o++] = b; idx[o++] = c; idx[o++] = d;
        }
      }
      return { v: v, i: idx };
    }

    function buildTentMesh() {
      var vertsPerRib = (TENT_SEG + 1) * 2;
      var v = new Float32Array(TENT_RIBS * vertsPerRib * 3);
      var idx = new Uint16Array(TENT_RIBS * TENT_SEG * 6);
      var o = 0, oi = 0, rib, k;
      for (rib = 0; rib < TENT_RIBS; rib++) {
        for (k = 0; k <= TENT_SEG; k++) {
          var t = k / TENT_SEG;
          v[o++] = t; v[o++] = -1; v[o++] = rib;
          v[o++] = t; v[o++] = 1; v[o++] = rib;
        }
        var base = rib * vertsPerRib;
        for (k = 0; k < TENT_SEG; k++) {
          var a = base + k * 2, b = a + 1, c = a + 2, d = a + 3;
          idx[oi++] = a; idx[oi++] = b; idx[oi++] = c;
          idx[oi++] = b; idx[oi++] = d; idx[oi++] = c;
        }
      }
      return { v: v, i: idx };
    }

    function staticBuf(target, data) {
      var b = gl.createBuffer();
      gl.bindBuffer(target, b);
      gl.bufferData(target, data, gl.STATIC_DRAW);
      return b;
    }

    /* Build/rebuild every GL resource. Returns false on ANY failure so the
       caller can route to the 2D fallback instead of a black screen. */
    function initGL() {
      progBack = makeProg(BACK_VS, BACK_FS, { a_p: 0 },
        ['u_shaft', 'u_activity', 'u_aspect']);
      progBell = makeProg(BELL_VS, BELL_FS, { a_pos: 0, a_norm: 1, a_i0: 2, a_i1: 3 },
        ['u_vp', 'u_cam', 'u_dim', 'u_vpw', 'u_time', 'u_activity', 'u_fogD']);
      progTent = makeProg(TENT_VS, TENT_FS, { a_trs: 0, a_i0: 2, a_i1: 3 },
        ['u_vp', 'u_cam', 'u_right', 'u_dim', 'u_vpw', 'u_activity', 'u_fogD']);
      progShell = makeProg(SHELL_VS, SHELL_FS, { a_q: 0, a_i0: 2, a_i1: 3 },
        ['u_vp', 'u_right', 'u_up', 'u_dimD']);
      progGlow = makeProg(GLOW_VS, GLOW_FS, { a_q: 0 },
        ['u_vp', 'u_pos', 'u_right', 'u_up', 'u_size', 'u_color', 'u_int']);
      if (!progBack || !progBell || !progTent || !progShell || !progGlow) { return false; }

      var bell = buildBellMesh();
      var tent = buildTentMesh();
      bellIdxCount = bell.i.length;
      tentIdxCount = tent.i.length;
      quadBuf = staticBuf(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]));
      bellVBuf = staticBuf(gl.ARRAY_BUFFER, bell.v);
      tentVBuf = staticBuf(gl.ARRAY_BUFFER, tent.v);
      bellIBuf = staticBuf(gl.ELEMENT_ARRAY_BUFFER, bell.i);
      tentIBuf = staticBuf(gl.ELEMENT_ARRAY_BUFFER, tent.i);
      instBuf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, instBuf);
      gl.bufferData(gl.ARRAY_BUFFER, instF.byteLength, gl.DYNAMIC_DRAW);
      shellBuf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, shellBuf);
      gl.bufferData(gl.ARRAY_BUFFER, shellF.byteLength, gl.DYNAMIC_DRAW);

      gl.disable(gl.DEPTH_TEST);
      gl.disable(gl.CULL_FACE);
      gl.clearColor(0.016, 0.039, 0.071, 1);
      if (gl.getError && gl.getError() !== gl.NO_ERROR && !gl.isContextLost()) { return false; }
      return true;
    }

    /* -------------------------------------------------------------- camera */
    function perspective(m, fovy, asp, near, far) {
      var f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
      m[0] = f / asp; m[1] = 0; m[2] = 0; m[3] = 0;
      m[4] = 0; m[5] = f; m[6] = 0; m[7] = 0;
      m[8] = 0; m[9] = 0; m[10] = (far + near) * nf; m[11] = -1;
      m[12] = 0; m[13] = 0; m[14] = 2 * far * near * nf; m[15] = 0;
    }

    function mul4(o, a, b) {
      var c, r;
      for (c = 0; c < 4; c++) {
        for (r = 0; r < 4; r++) {
          o[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
        }
      }
    }

    function computeRay(nx, ny) {
      var vx = nx * tanXA, vy = ny * tanY;
      var dx = rgt[0] * vx + upv[0] * vy - bck[0];
      var dy = rgt[1] * vx + upv[1] * vy - bck[1];
      var dz = rgt[2] * vx + upv[2] * vy - bck[2];
      var il = 1 / Math.sqrt(dx * dx + dy * dy + dz * dz);
      urx = dx * il; ury = dy * il; urz = dz * il;
    }

    function updateCamera(dt) {
      var ty = mouseIn ? mouseNX * MAXTILT : 0;
      var tp = mouseIn ? mouseNY * MAXTILT : 0;
      yaw += (ty - yaw) * Math.min(1, dt * 2.2);
      pitch += (tp - pitch) * Math.min(1, dt * 2.2);
      var swy = yaw + Math.sin(timeS * 0.11) * 0.012;   /* idle sway */
      var swp = pitch + Math.cos(timeS * 0.14) * 0.009;
      eyeX = Math.sin(timeS * 0.13) * 0.45;
      eyeY = Math.cos(timeS * 0.17) * 0.3;
      eyeZ = 0;
      var cy = Math.cos(swy), sy = Math.sin(swy);
      var cp = Math.cos(swp), sp = Math.sin(swp);
      rgt[0] = cy; rgt[1] = 0; rgt[2] = -sy;
      upv[0] = sy * sp; upv[1] = cp; upv[2] = cy * sp;
      bck[0] = sy * cp; bck[1] = -sp; bck[2] = cy * cp;
      mView[0] = rgt[0]; mView[4] = rgt[1]; mView[8] = rgt[2];
      mView[12] = -(rgt[0] * eyeX + rgt[1] * eyeY + rgt[2] * eyeZ);
      mView[1] = upv[0]; mView[5] = upv[1]; mView[9] = upv[2];
      mView[13] = -(upv[0] * eyeX + upv[1] * eyeY + upv[2] * eyeZ);
      mView[2] = bck[0]; mView[6] = bck[1]; mView[10] = bck[2];
      mView[14] = -(bck[0] * eyeX + bck[1] * eyeY + bck[2] * eyeZ);
      mView[3] = 0; mView[7] = 0; mView[11] = 0; mView[15] = 1;
      mul4(mVP, mProj, mView);
      computeRay(mouseNX, mouseNY);
      rayDX = urx; rayDY = ury; rayDZ = urz;
    }

    /* ------------------------------------------------------------ populate */
    function initJelly(j, near) {
      j.near = near;
      if (near) {
        j.z = -(5.2 + Math.random() * 3.4);
        j.scale = 1.0 + Math.random() * 0.45;
        j.glow = 0.42 + Math.random() * 0.12;
      } else {
        j.z = -(8.5 + Math.random() * 28);
        j.scale = 0.5 + Math.random() * 0.6;
        j.glow = 0.8 + Math.random() * 0.4;
      }
      var hw = tanXA * (-j.z) * 1.1;
      var hh = tanY * (-j.z) * 1.1;
      j.x = (Math.random() * 2 - 1) * hw;
      j.y = (Math.random() * 2 - 1) * hh;
      j.speed = (0.28 + Math.random() * 0.35) * (near ? 0.6 : 1);
      j.kick = (0.5 + Math.random() * 0.45) * (near ? 0.6 : 1);
      j.phase = Math.random() * Math.PI * 2;
      j.phaseRate = (0.9 + Math.random() * 0.7) * (near ? 0.6 : 1);
      j.wobPhase = Math.random() * Math.PI * 2;
      j.wobRate = 0.25 + Math.random() * 0.35;
      j.wobAmp = 0.35 + Math.random() * 0.5;
      j.vx = 0; j.vy = 0; j.vz = 0;
      j.ox = 0; j.oy = 0; j.oz = 0;
      j.tox = 0; j.toy = 0; j.toz = 0;
      j.bright = 1; j.brightT = 1;
      j.colorMix = Math.random();
      j.seed = Math.random() * Math.PI * 2;
    }

    function populate() {
      var mobile = W <= 720;
      var n = mobile ? 22 : 46;
      var nears = mobile ? 2 : 4;
      jellyCount = Math.min(MAX_JELLY, n + nears);
      var i;
      for (i = 0; i < jellyCount; i++) {
        initJelly(jellies[i], i >= jellyCount - nears);
      }
      instView = instF.subarray(0, jellyCount * 8);
    }

    /* ----------------------------------------------------------------- sim */
    function sim(dt, nowMs) {
      activity += (activityTarget - activity) * Math.min(1, dt * 1.6);
      if (Math.abs(activityTarget - activity) < 0.002) { activity = activityTarget; }

      var wantCursor = mouseIn && (nowMs - lastMouseT < 2000);
      cursorA += ((wantCursor ? 1 : 0) - cursorA) * Math.min(1, dt * 4);

      updateCamera(dt);

      var si;
      for (si = 0; si < MAX_SHELL; si++) {
        var sh = shells[si];
        if (!sh.active) { continue; }
        if (sh.delay > 0) { sh.delay -= dt; continue; }
        sh.r += sh.speed * dt;
        var f = 1 - sh.r / sh.maxR;
        if (f <= 0) { sh.active = false; continue; }
        sh.alpha = f * f * Math.min(1, sh.strength);
      }
      if (flash.active) {
        flash.t += dt;
        if (flash.t >= flash.dur) { flash.active = false; }
      }

      var speedMul = 0.55 + activity * 1.5;
      var i;
      for (i = 0; i < jellyCount; i++) {
        var j = jellies[i];
        j.phase += j.phaseRate * (0.65 + activity * 0.9) * dt;
        j.wobPhase += j.wobRate * dt;
        var sp = Math.sin(j.phase);
        var ctr = sp > 0 ? sp * sp : 0;

        var decay = Math.max(0, 1 - dt * 2.4);
        j.vx *= decay; j.vy *= decay; j.vz *= decay;

        j.x += (Math.sin(j.wobPhase) * j.wobAmp * 0.55 + j.vx) * dt;
        j.y += ((j.speed + j.kick * ctr) * speedMul + j.vy) * dt;
        j.z += (Math.cos(j.wobPhase * 0.8 + j.seed) * j.wobAmp * 0.2 + j.vz) * dt;
        if (j.z > -3.5) { j.z = -3.5; } else if (j.z < -38) { j.z = -38; }

        /* wrap: rises past the frustum top -> respawn below */
        var hw = tanXA * (-j.z) * 1.2 + j.scale;
        var hh = tanY * (-j.z) * 1.2 + j.scale;
        if (j.y - eyeY > hh + 2) {
          j.y = eyeY - hh - 2;
          j.x = eyeX + (Math.random() * 2 - 1) * hw;
        } else if (j.y - eyeY < -(hh + 4)) {
          j.y = eyeY - hh - 2;
        }
        if (j.x - eyeX < -hw) { j.x = eyeX + hw; }
        else if (j.x - eyeX > hw) { j.x = eyeX - hw; }

        /* pointer ray flow field: push perpendicular to the view ray */
        j.tox = 0; j.toy = 0; j.toz = 0;
        var bT = 1;
        if (mouseIn) {
          var rx = j.x - eyeX, ry = j.y - eyeY, rz = j.z - eyeZ;
          var along = rx * rayDX + ry * rayDY + rz * rayDZ;
          if (along > 1) {
            var qx = rx - rayDX * along, qy = ry - rayDY * along, qz = rz - rayDZ * along;
            var q2 = qx * qx + qy * qy + qz * qz;
            var R = 1.5 + along * 0.16;             /* depth-scaled radius */
            if (q2 < R * R && q2 > 0.0001) {
              var qd = Math.sqrt(q2);
              var tt = 1 - qd / R;
              tt = tt * tt * (3 - 2 * tt);
              var push = (0.7 + along * 0.06) * tt;
              j.tox = qx / qd * push;
              j.toy = qy / qd * push;
              j.toz = qz / qd * push;
              bT = 1 + 0.5 * tt;                     /* brighten up to 1.5x */
            }
          }
        }
        j.ox += (j.tox - j.ox) * Math.min(1, dt * 4.2);
        j.oy += (j.toy - j.oy) * Math.min(1, dt * 4.2);
        j.oz += (j.toz - j.oz) * Math.min(1, dt * 4.2);

        /* shockwave wavefronts push + flash as they pass (in 3D) */
        for (si = 0; si < MAX_SHELL; si++) {
          var sh2 = shells[si];
          if (!sh2.active || sh2.delay > 0) { continue; }
          var ddx = j.x - sh2.x, ddy = j.y - sh2.y, ddz = j.z - sh2.z;
          var band = 2.5;
          var lo = sh2.r - band, hi = sh2.r + band;
          var dd2 = ddx * ddx + ddy * ddy + ddz * ddz;
          if (dd2 > hi * hi || (lo > 0 && dd2 < lo * lo)) { continue; }
          var dd = Math.sqrt(dd2);
          if (dd < 0.05) { continue; }
          var w = 1 - Math.abs(dd - sh2.r) / band;
          w = w * w * sh2.alpha;
          var imp = w * 22 * dt;
          j.vx += ddx / dd * imp;
          j.vy += ddy / dd * imp;
          j.vz += ddz / dd * imp;
          var rb = 1 + w * 1.1;
          if (rb > bT) { bT = rb; }
        }

        j.brightT = bT;
        j.bright += (j.brightT - j.bright) * Math.min(1, dt * 3);

        var o = i * 8;
        instF[o] = j.x + j.ox;
        instF[o + 1] = j.y + j.oy;
        instF[o + 2] = j.z + j.oz;
        instF[o + 3] = j.scale;
        instF[o + 4] = j.phase;
        instF[o + 5] = j.colorMix;
        instF[o + 6] = j.glow * j.bright;
        instF[o + 7] = j.seed;
      }

      /* pack visible shells for the ring draw */
      shellCount = 0;
      for (si = 0; si < MAX_SHELL; si++) {
        var sh3 = shells[si];
        if (!sh3.active || sh3.delay > 0 || sh3.r <= 0.05) { continue; }
        var so = shellCount * 8;
        shellF[so] = sh3.x; shellF[so + 1] = sh3.y; shellF[so + 2] = sh3.z;
        shellF[so + 3] = sh3.r / 0.82;   /* quad radius so the ring peak sits at r */
        shellF[so + 4] = sh3.alpha * 0.9;
        shellF[so + 5] = 0; shellF[so + 6] = 0; shellF[so + 7] = 0;
        shellCount++;
      }
    }

    /* ---------------------------------------------------------------- draw */
    function bindQuad() {
      gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
      setDiv(0, 0);
    }

    function bindInstances() {
      gl.bindBuffer(gl.ARRAY_BUFFER, instBuf);
      gl.enableVertexAttribArray(2);
      gl.vertexAttribPointer(2, 4, gl.FLOAT, false, 32, 0);
      setDiv(2, 1);
      gl.enableVertexAttribArray(3);
      gl.vertexAttribPointer(3, 4, gl.FLOAT, false, 32, 16);
      setDiv(3, 1);
    }

    function unbindInstances() {
      setDiv(2, 0); gl.disableVertexAttribArray(2);
      setDiv(3, 0); gl.disableVertexAttribArray(3);
    }

    function drawGlowQuad(x, y, z, size, cr, cg, cb, intensity) {
      gl.useProgram(progGlow.p);
      gl.uniformMatrix4fv(progGlow.u_vp, false, mVP);
      gl.uniform3f(progGlow.u_pos, x, y, z);
      gl.uniform3f(progGlow.u_right, rgt[0], rgt[1], rgt[2]);
      gl.uniform3f(progGlow.u_up, upv[0], upv[1], upv[2]);
      gl.uniform1f(progGlow.u_size, size);
      gl.uniform3f(progGlow.u_color, cr, cg, cb);
      gl.uniform1f(progGlow.u_int, intensity);
      bindQuad();
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }

    function draw() {
      gl.viewport(0, 0, canvas.width, canvas.height);

      /* backdrop: opaque gradient + animated light shafts */
      gl.disable(gl.BLEND);
      gl.useProgram(progBack.p);
      gl.uniform4f(progBack.u_shaft,
        Math.sin(timeS * 0.31), Math.sin(timeS * 0.23 + 2.1),
        Math.sin(timeS * 0.27 + 4.2), Math.sin(timeS * 0.19 + 1.1));
      gl.uniform1f(progBack.u_activity, activity);
      gl.uniform1f(progBack.u_aspect, aspect);
      bindQuad();
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

      /* everything luminous is additive */
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ONE);

      /* cursor light billboard at mid-depth along the pointer ray */
      if (cursorA > 0.01 && urz < -0.001) {
        var ct = (CURSOR_Z - eyeZ) / urz;
        drawGlowQuad(eyeX + urx * ct, eyeY + ury * ct, eyeZ + urz * ct,
          3.2 + activity * 1.0, 0.35, 0.62, 0.80, cursorA * 0.30);
      }

      /* upload per-instance data once; tentacles + bells share it */
      gl.bindBuffer(gl.ARRAY_BUFFER, instBuf);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, instView);

      /* tentacle ribbons (one instanced draw for every ribbon of every jelly) */
      gl.useProgram(progTent.p);
      gl.uniformMatrix4fv(progTent.u_vp, false, mVP);
      gl.uniform3f(progTent.u_cam, eyeX, eyeY, eyeZ);
      gl.uniform3f(progTent.u_right, rgt[0], rgt[1], rgt[2]);
      gl.uniform2f(progTent.u_dim, dimL, dimR);
      gl.uniform1f(progTent.u_vpw, W);
      gl.uniform1f(progTent.u_activity, activity);
      gl.uniform1f(progTent.u_fogD, FOG_D);
      gl.bindBuffer(gl.ARRAY_BUFFER, tentVBuf);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
      setDiv(0, 0);
      gl.disableVertexAttribArray(1);
      bindInstances();
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, tentIBuf);
      drawElemInst(tentIdxCount, jellyCount);
      unbindInstances();

      /* bells (one instanced draw) */
      gl.useProgram(progBell.p);
      gl.uniformMatrix4fv(progBell.u_vp, false, mVP);
      gl.uniform3f(progBell.u_cam, eyeX, eyeY, eyeZ);
      gl.uniform2f(progBell.u_dim, dimL, dimR);
      gl.uniform1f(progBell.u_vpw, W);
      gl.uniform1f(progBell.u_time, timeS);
      gl.uniform1f(progBell.u_activity, activity);
      gl.uniform1f(progBell.u_fogD, FOG_D);
      gl.bindBuffer(gl.ARRAY_BUFFER, bellVBuf);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 24, 0);
      setDiv(0, 0);
      gl.enableVertexAttribArray(1);
      gl.vertexAttribPointer(1, 3, gl.FLOAT, false, 24, 12);
      setDiv(1, 0);
      bindInstances();
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, bellIBuf);
      drawElemInst(bellIdxCount, jellyCount);
      unbindInstances();
      gl.disableVertexAttribArray(1);

      /* shockwave shells (one instanced draw, <= 6 quads) */
      if (shellCount > 0) {
        gl.useProgram(progShell.p);
        gl.uniformMatrix4fv(progShell.u_vp, false, mVP);
        gl.uniform3f(progShell.u_right, rgt[0], rgt[1], rgt[2]);
        gl.uniform3f(progShell.u_up, upv[0], upv[1], upv[2]);
        gl.uniform3f(progShell.u_dimD, dimL * DPR, dimR * DPR, 120 * DPR);
        bindQuad();
        gl.bindBuffer(gl.ARRAY_BUFFER, shellBuf);
        gl.bufferSubData(gl.ARRAY_BUFFER, 0, shellF);
        gl.enableVertexAttribArray(2);
        gl.vertexAttribPointer(2, 4, gl.FLOAT, false, 32, 0);
        setDiv(2, 1);
        gl.enableVertexAttribArray(3);
        gl.vertexAttribPointer(3, 4, gl.FLOAT, false, 32, 16);
        setDiv(3, 1);
        drawArrInst(4, shellCount);
        unbindInstances();
      }

      /* sonar flash at the pulse origin */
      if (flash.active) {
        var ft = 1 - flash.t / flash.dur;
        drawGlowQuad(flash.x, flash.y, flash.z,
          3.0 + (1 - ft) * 4.5, 0.55, 0.83, 0.95, ft * ft * 0.85);
      }
    }

    /* ---------------------------------------------------------------- loop */
    function frame(t) {
      rafId = 0;
      if (!running) { return; }
      var dt = (t - lastT) / 1000;
      lastT = t;
      if (dt > 0.05) { dt = 0.05; }
      timeS += dt;
      if (dt > 0) { sim(dt, t); }
      draw();
      rafId = requestAnimationFrame(frame);
    }

    function start() {
      if (running || !mounted || dead || contextLost) { return; }
      running = true;
      lastT = performance.now();
      if (!rafId) { rafId = requestAnimationFrame(frame); }
    }

    function stop() {
      running = false;
      if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    }

    /* ----------------------------------------------------------- mgmt/events */
    function measureColumn() {
      var col = document.querySelector('.column');
      if (col) {
        var r = col.getBoundingClientRect();
        dimL = r.left - 60;
        dimR = r.right;
      } else {
        dimL = 0; dimR = -1;   /* disabled */
      }
    }

    function applyResizeNow(force) {
      if (!canvas || !gl) { return; }
      var rect = canvas.getBoundingClientRect();
      var newW = rect.width || window.innerWidth;
      var newH = rect.height || window.innerHeight;
      var newDPR = Math.min(2, window.devicePixelRatio || 1);
      measureColumn();
      if (!force && newW === W && newH === H && newDPR === DPR) { return; }
      var oldW = W;
      W = newW; H = newH; DPR = newDPR;
      canvas.width = Math.max(1, Math.round(W * DPR));
      canvas.height = Math.max(1, Math.round(H * DPR));
      aspect = W / Math.max(1, H);
      tanXA = tanY * aspect;
      perspective(mProj, FOVY, aspect, NEARZ, FARZ);
      var wasMobile = oldW > 0 && oldW <= 720;
      var isMobile = W <= 720;
      if (jellyCount === 0 || oldW <= 0 || wasMobile !== isMobile) { populate(); }
    }

    var resizeTimer = 0;
    function onResize() {
      if (resizeTimer) { clearTimeout(resizeTimer); }
      resizeTimer = setTimeout(function () { resizeTimer = 0; applyResizeNow(false); }, 150);
    }

    function onMouseMove(e) {
      mouseNX = (e.clientX / Math.max(1, W)) * 2 - 1;
      mouseNY = 1 - (e.clientY / Math.max(1, H)) * 2;
      mouseIn = true;
      lastMouseT = performance.now();
    }

    function onMouseOut(e) {
      if (!e.relatedTarget) { mouseIn = false; }
    }

    function onVisibility() {
      if (document.hidden) { stop(); }
      else { start(); }
    }

    function fatal() {
      dead = true;
      stop();
      if (typeof api.onFatal === 'function') { api.onFatal(); }
    }

    function onCtxLost(e) {
      e.preventDefault();
      contextLost = true;
      stop();
    }

    function onCtxRestored() {
      contextLost = false;
      if (!isGL2) {
        /* WebGL extension objects are invalidated by context loss: a stale
           ANGLE_instanced_arrays ext silently no-ops every instanced draw
           (no GL error). Re-fetch it before rebuilding resources; a null
           result means instancing is gone -> demote to the 2D fallback. */
        instExt = gl ? gl.getExtension('ANGLE_instanced_arrays') : null;
        if (!instExt) { fatal(); return; }
      }
      var ok = false;
      try { ok = initGL(); } catch (err) { ok = false; }
      if (!ok) { fatal(); return; }
      applyResizeNow(true);
      start();
    }

    /* ----------------------------------------------------------------- API */
    /* mount() returns false on ANY setup failure (no context, missing
       instancing extension on webgl1, shader compile/link failure) so the
       selector can fall back to the 2D renderer. */
    function mount(canvasEl) {
      if (dead || mounted) { return false; }
      if (!canvasEl || typeof canvasEl.getContext !== 'function') { return false; }
      try {
        canvas = canvasEl;
        var attrs = {
          alpha: false, antialias: true, depth: false, stencil: false,
          premultipliedAlpha: true, preserveDrawingBuffer: false,
          powerPreference: 'high-performance'
        };
        gl = canvas.getContext('webgl2', attrs);
        isGL2 = !!gl;
        if (!gl) {
          gl = canvas.getContext('webgl', attrs) || canvas.getContext('experimental-webgl', attrs);
          if (gl) {
            instExt = gl.getExtension('ANGLE_instanced_arrays');
            if (!instExt) { gl = null; canvas = null; return false; }
          }
        }
        if (!gl || gl.isContextLost()) { gl = null; canvas = null; return false; }
        if (!initGL()) { gl = null; canvas = null; return false; }
      } catch (err) {
        gl = null; canvas = null;
        return false;
      }

      canvas.addEventListener('webglcontextlost', onCtxLost, false);
      canvas.addEventListener('webglcontextrestored', onCtxRestored, false);
      window.addEventListener('mousemove', onMouseMove, { passive: true });
      window.addEventListener('mouseout', onMouseOut, { passive: true });
      window.addEventListener('resize', onResize);
      document.addEventListener('visibilitychange', onVisibility);

      mounted = true;
      contextLost = false;       /* a loss on a PREVIOUS canvas must not gate
                                    this fresh context (demote->repromote) */
      W = 0; H = 0;              /* force the first resize to apply */
      applyResizeNow(true);
      updateCamera(0);
      start();
      return true;
    }

    function pulse(clientX, clientY, strength) {
      if (!mounted || dead || contextLost) { return; }
      var s = (typeof strength === 'number' && isFinite(strength)) ? strength : 1;
      var x = typeof clientX === 'number' ? clientX : W * 0.5;
      var y = typeof clientY === 'number' ? clientY : H * 0.5;
      var nx = (x / Math.max(1, W)) * 2 - 1;
      var ny = 1 - (y / Math.max(1, H)) * 2;
      computeRay(nx, ny);
      var t = urz < -0.001 ? (PULSE_Z - eyeZ) / urz : -PULSE_Z;
      var px = eyeX + urx * t, py = eyeY + ury * t, pz = eyeZ + urz * t;
      var rings = s >= 2 ? 3 : 1;
      var k;
      for (k = 0; k < rings; k++) {
        /* claim a free slot, else recycle the most-expired shell (cap 6) */
        var slot = null, oldest = null, oldestF = -1, si;
        for (si = 0; si < MAX_SHELL; si++) {
          var sh = shells[si];
          if (!sh.active) { slot = sh; break; }
          var fexp = sh.maxR > 0 ? sh.r / sh.maxR : 1;
          if (fexp > oldestF) { oldestF = fexp; oldest = sh; }
        }
        if (!slot) { slot = oldest; }
        slot.active = true;
        slot.x = px; slot.y = py; slot.z = pz;
        slot.r = 0;
        slot.maxR = (rings > 1 ? 42 : 15) * (0.75 + Math.min(s, 3) * 0.18);
        slot.speed = (rings > 1 ? 20 : 15) * (0.8 + Math.min(s, 3) * 0.12);
        slot.delay = k * 0.18;                     /* staggered ~180ms */
        slot.strength = Math.min(s, 3) * (1 - k * 0.22);
        slot.alpha = 0;
      }
      if (s >= 2) {
        flash.active = true;
        flash.x = px; flash.y = py; flash.z = pz;
        flash.t = 0;
      }
    }

    function setActivity(level) {
      var v = (typeof level === 'number' && isFinite(level)) ? level : 0;
      activityTarget = v < 0 ? 0 : (v > 1 ? 1 : v);
    }

    function detach() {
      stop();
      if (resizeTimer) { clearTimeout(resizeTimer); resizeTimer = 0; }
      if (mounted) {
        window.removeEventListener('mousemove', onMouseMove);
        window.removeEventListener('mouseout', onMouseOut);
        window.removeEventListener('resize', onResize);
        document.removeEventListener('visibilitychange', onVisibility);
        if (canvas) {
          canvas.removeEventListener('webglcontextlost', onCtxLost, false);
          canvas.removeEventListener('webglcontextrestored', onCtxRestored, false);
        }
      }
      mounted = false;
      canvas = null;
      gl = null;
      instExt = null;
    }

    return api;
  }

  /* =============================================================== selector == */
  /* Owns window.Jellyfield. Picks the renderer at mount, re-selects when
   * prefers-reduced-motion flips (both directions), and demotes 3D -> 2D when
   * the GL context dies for good. A canvas only ever yields ONE context kind,
   * so every swap replaces the canvas element with a fresh clone first. */
  var sel2d = null;
  var sel3d = null;
  var selActive = null;
  var selKind = '';
  var selCanvas = null;
  var selMql = null;
  var selLastActivity = 0;

  function selFreshCanvas() {
    if (!selCanvas) { return null; }
    var parent = selCanvas.parentNode;
    if (!parent) { return selCanvas; }   /* best effort: cannot swap a detached node */
    var fresh = selCanvas.cloneNode(false);   /* keeps id/class/attrs */
    parent.replaceChild(fresh, selCanvas);
    selCanvas = fresh;
    return fresh;
  }

  function selUse2D() {
    if (!sel2d) { sel2d = createJellyfield2D(); }
    selActive = sel2d;
    selKind = '2d';
    sel2d.mount(selCanvas);
    sel2d.setActivity(selLastActivity);
  }

  function selTry3D() {
    if (!sel3d) {
      sel3d = createJellyfield3D();
      sel3d.onFatal = selOnFatal;
    }
    if (sel3d.mount(selCanvas)) {
      selActive = sel3d;
      selKind = '3d';
      sel3d.setActivity(selLastActivity);
      return true;
    }
    return false;
  }

  function selOnFatal() {
    /* 3D context died and could not be restored: demote to 2D for the session.
       The old canvas is locked to its dead WebGL context — swap in a fresh one. */
    if (selKind !== '3d') { return; }
    if (sel3d) { sel3d.detach(); }
    selFreshCanvas();
    selUse2D();
  }

  function selReduced() {
    return !!(selMql && selMql.matches);
  }

  function selOnMotionChange() {
    if (!selCanvas || !selKind) { return; }
    if (selReduced() && selKind === '3d') {
      /* 3D has no static mode: hand the field to the 2D renderer. */
      sel3d.detach();
      selFreshCanvas();
      selUse2D();
    } else if (!selReduced() && selKind === '2d') {
      /* motion allowed again: try to promote back to 3D. */
      sel2d.detach();
      selFreshCanvas();
      if (!selTry3D()) {
        selFreshCanvas();   /* the failed 3D attempt may have claimed a GL context */
        selUse2D();
      }
    }
    /* reduced with 2D active: the 2D engine renders its own static field. */
  }

  function selMount(canvasEl) {
    if (!canvasEl || typeof canvasEl.getContext !== 'function') { return; }
    if (selCanvas && selActive) { return; }   /* one mount per page */
    selCanvas = canvasEl;
    if (!selMql && window.matchMedia) {
      selMql = window.matchMedia('(prefers-reduced-motion: reduce)');
      if (selMql) {
        if (typeof selMql.addEventListener === 'function') { selMql.addEventListener('change', selOnMotionChange); }
        else if (typeof selMql.addListener === 'function') { selMql.addListener(selOnMotionChange); }
      }
    }
    if (selReduced()) { selUse2D(); return; }
    if (!selTry3D()) {
      selFreshCanvas();   /* the failed 3D attempt may have claimed a GL context */
      selUse2D();
    }
  }

  function selPulse(clientX, clientY, strength) {
    if (selActive) { selActive.pulse(clientX, clientY, strength); }
  }

  function selSetActivity(level) {
    var v = (typeof level === 'number' && isFinite(level)) ? level : 0;
    selLastActivity = v < 0 ? 0 : (v > 1 ? 1 : v);
    if (selActive) { selActive.setActivity(level); }
  }

  window.Jellyfield = { mount: selMount, pulse: selPulse, setActivity: selSetActivity };
})();
