/* Jellyfield HD — WebGL2 HDR multi-pass jellyfish.
 * Spec: docs/superpowers/specs/2026-09-29-jellyfield-hd-design.md
 * Registers window.JellyfieldHD; the selector in jellyfield.js owns window.Jellyfield.
 * The engine is species-agnostic (spec, "The species contract"). It was built
 * for a two-species bake-off: comments that say "A" mean candidate A, the
 * moon jelly (retired along with the old 3D engine), and "B" means the
 * purple-striped jelly in jellyfield-species-striped.js, the one creature
 * that ships. The contract surface only A used (the 'fringe' chain kind,
 * goldFerrules) stays: it is documented contract, not dead code. */
(function () {
  'use strict';

  var SPECIES = {};

  /* the one creature (the owner's pick): its species file registers it in
     SPECIES under this id. There is no species switch any more. */
  var SPECIES_ID = 'striped';

  /* test knobs, read once */
  var PARAMS = (function () {
    var q = { tier: null, slow: null, debug: false, ldr: false };
    try {
      var sp = new URLSearchParams(window.location.search);
      /* only a real tier name (the keys of TIERS in the engine): anything
         else, ?jellytier=constructor included, means absent */
      var tierQ = (sp.get('jellytier') || '').toLowerCase();
      q.tier = (tierQ === 'ultra' || tierQ === 'high' || tierQ === 'low') ? tierQ : null;
      /* a synthetic frame time in ms. A bare ?jellyslow reads as 0 and garbage
         as NaN, and either would sit the tier EMA under the step-up line for
         the session, so anything but a positive finite number means absent */
      var slow = sp.has('jellyslow') ? Number(sp.get('jellyslow')) : NaN;
      q.slow = (isFinite(slow) && slow > 0) ? slow : null;
      q.debug = sp.get('jellydebug') === '1';
      q.ldr = sp.get('jellyldr') === '1';
    } catch (_) { /* old browsers: defaults */ }
    return q;
  })();

  function pickSpecies() {
    /* no species file loaded: mount() fails and the selector falls to 2D */
    return SPECIES[SPECIES_ID] || null;
  }

  function nowMs() {
    return (window.performance && typeof performance.now === 'function') ? performance.now() : Date.now();
  }

  function createJellyfieldHD() {
    var canvas = null, gl = null, mounted = false, rafId = 0, frames = 0, running = false;
    var species = null;
    var api = { mount: mount, pulse: pulse, setActivity: setActivity, detach: detach, info: info, onFatal: null };

    /* ---------------------------------------------------- capabilities & size */
    /* 8,294,400 = 3840 x 2160: the drawing buffer never exceeds 4K-at-1x worth
       of pixels, whatever the CSS size and DPR say (a 4K display at 2x would
       otherwise ask for 33 MP of HDR targets per pass). */
    var PIXEL_CAP = 8294400;
    var caps = { hdr: false, maxSamples: 0, maxTex: 0 };
    var cssW = 0, cssH = 0, dprEff = 1, drawW = 0, drawH = 0;
    var contextLost = false, lostTimer = 0, lostVisibleMs = 0, LOST_GRACE_MS = 3000, dead = false;
    var LOST_POLL_MS = 250, lostTick = 0;
    var resizeTimer = 0, RESIZE_MS = 150;

    function detectCaps() {
      /* HDR needs float colour attachments; without them the pipeline runs in
         LDR mode (SRGB8_ALPHA8 + pre-exposure). ?jellyldr=1 forces that path so it
         can be tested on hardware that has the extension. */
      caps.hdr = !window.JellyfieldHD.params.ldr && !!gl.getExtension('EXT_color_buffer_float');
      gl.getExtension('OES_texture_float_linear');   /* optional: smoother HDR sampling */
      caps.maxSamples = gl.getParameter(gl.MAX_SAMPLES) || 0;
      caps.maxTex = gl.getParameter(gl.MAX_TEXTURE_SIZE) || 4096;
    }

    function computeSize() {
      var rect = canvas.getBoundingClientRect();
      cssW = rect.width || window.innerWidth;
      cssH = rect.height || window.innerHeight;
      dprEff = Math.min(3, window.devicePixelRatio || 1);
      var w = cssW * dprEff, h = cssH * dprEff;
      /* over the cap: shrink both axes by the same factor so the aspect holds,
         and dprEff with them so it stays the TRUE css->buffer ratio that
         drawW = round(cssW * dprEff) promises (pointer mapping and info()
         read it; a 4K display at 2x is really rendered at 1x) */
      if (w * h > PIXEL_CAP) { var k = Math.sqrt(PIXEL_CAP / (w * h)); w *= k; h *= k; dprEff *= k; }
      drawW = Math.max(1, Math.min(caps.maxTex, Math.round(w)));
      drawH = Math.max(1, Math.min(caps.maxTex, Math.round(h)));
      canvas.width = drawW; canvas.height = drawH;
    }

    /* ------------------------------------------------------- GL resources */
    /* Every GL object the engine makes goes through this registry, so a
       context loss can forget the whole set in one go (a restore hands back a
       context that never saw them; deleting them there only logs "object does
       not belong to this context") and detach() can free everything without
       each pass keeping its own list. */
    var res = (function () {
      var owned = [];
      function track(kind, obj) { owned.push([kind, obj]); return obj; }
      function del(kind, x) {
        if (kind === 'program') { gl.deleteProgram(x); }
        else if (kind === 'buffer') { gl.deleteBuffer(x); }
        else if (kind === 'vao') { gl.deleteVertexArray(x); }
        else if (kind === 'texture') { gl.deleteTexture(x); }
        else if (kind === 'fbo') { gl.deleteFramebuffer(x); }
        else if (kind === 'rb') { gl.deleteRenderbuffer(x); }
      }
      return {
        program: function (vs, fs, attribs) {
          function sh(type, src) {
            var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
            return s;
          }
          var p = gl.createProgram();
          var v = sh(gl.VERTEX_SHADER, vs), f = sh(gl.FRAGMENT_SHADER, fs);
          gl.attachShader(p, v); gl.attachShader(p, f);
          var name;
          for (name in attribs) { if (Object.prototype.hasOwnProperty.call(attribs, name)) { gl.bindAttribLocation(p, attribs[name], name); } }
          gl.linkProgram(p);
          /* ONE synchronous query per program. Every status query is a
             round trip to the GPU process, and when another page in the
             same browser is mid-frame there, each one waits for that frame:
             the three per program this used to make (two COMPILE_STATUS,
             one LINK_STATUS) x 13 programs held a second tab's load event
             for 20-30 s under SwiftShader. A failed compile fails the link,
             so the link status covers both; the shader logs are fetched only
             then, for the message. A lost context fails every link: not a
             shader bug, so no throw. */
          if (!gl.getProgramParameter(p, gl.LINK_STATUS) && !gl.isContextLost()) {
            var log = 'link: ' + gl.getProgramInfoLog(p);
            if (!gl.getShaderParameter(v, gl.COMPILE_STATUS)) { log += ' | vs: ' + gl.getShaderInfoLog(v); }
            if (!gl.getShaderParameter(f, gl.COMPILE_STATUS)) { log += ' | fs: ' + gl.getShaderInfoLog(f); }
            gl.deleteShader(v); gl.deleteShader(f); gl.deleteProgram(p);
            throw new Error(log);
          }
          gl.deleteShader(v); gl.deleteShader(f);
          var u = {}, n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS), i;
          for (i = 0; i < n; i++) {
            var a = gl.getActiveUniform(p, i);
            var key = a.name.replace(/\[0\]$/, '');   /* uniform arrays report as name[0] */
            u[key] = gl.getUniformLocation(p, a.name);
          }
          track('program', p);
          return { p: p, u: u };
        },
        buffer: function () { return track('buffer', gl.createBuffer()); },
        vao: function () { return track('vao', gl.createVertexArray()); },
        /* fmt is the sized internal format: RGBA16F in HDR, SRGB8_ALPHA8 in LDR */
        texture: function (w, h, fmt) {
          var t = gl.createTexture();
          gl.bindTexture(gl.TEXTURE_2D, t);
          gl.texImage2D(gl.TEXTURE_2D, 0, fmt, w, h, 0, gl.RGBA,
                        fmt === gl.RGBA16F ? gl.HALF_FLOAT : gl.UNSIGNED_BYTE, null);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
          return track('texture', t);
        },
        fbo: function (tex) {
          var f = gl.createFramebuffer();
          gl.bindFramebuffer(gl.FRAMEBUFFER, f);
          gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
          return track('fbo', f);
        },
        /* the same fmt as the texture it resolves into: blitFramebuffer requires it */
        msaaFbo: function (w, h, samples, fmt) {
          var rb = track('rb', gl.createRenderbuffer());
          gl.bindRenderbuffer(gl.RENDERBUFFER, rb);
          gl.renderbufferStorageMultisample(gl.RENDERBUFFER, samples, fmt, w, h);
          var f = track('fbo', gl.createFramebuffer());
          gl.bindFramebuffer(gl.FRAMEBUFFER, f);
          gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.RENDERBUFFER, rb);
          return { fbo: f, rb: rb };
        },
        /* size-dependent objects are released one by one when resizing */
        release: function (obj) {
          var i;
          for (i = owned.length - 1; i >= 0; i--) {
            if (owned[i][1] === obj) {
              if (gl && !gl.isContextLost()) { del(owned[i][0], obj); }
              owned.splice(i, 1);
              return;
            }
          }
        },
        releaseAll: function () {
          if (gl && !gl.isContextLost()) {
            gl.useProgram(null);   /* a program in use is only flagged, not freed */
            owned.forEach(function (o) { del(o[0], o[1]); });
          }
          owned.length = 0;   /* after a loss nothing belongs to the live context */
        },
        /* drop the bookkeeping with no GL calls at all (context just died) */
        forget: function () { owned.length = 0; }
      };
    })();

    /* ------------------------------------------------------------ constants */
    var TWO_PI = 6.283185307179586;
    var FOVY = 55 * Math.PI / 180;
    var NEARZ = 0.5, FARZ = 80;
    var FOG_D = 0.048;               /* exponential fog toward the abyss */
    var MAXTILT = 3 * Math.PI / 180; /* mouse parallax: +-3 degrees */
    var HERO_Z = -8.2;               /* the creature's depth */
    var PULSE_Z = -13;               /* shockwaves detonate at mid-depth */
    var CURSOR_Z = -10;              /* cursor light lives at mid-depth */
    var N_SIL = 6;                   /* distant silhouettes */
    var SIL_SEG = 32, SIL_RINGS = 16;/* their (t, phi) grid: ~50 px wide at that depth */
    var MAX_SHELL = 6;               /* sonar shells in flight */
    var MAX_BLOOM = 6;               /* the Ultra chain; every tier fits in it */
    var DIM_FEATHER = 120;           /* the text-column band's feather, CSS px (as in the 2D engine) */

    /* THE CREATURE'S BOUNDED CLOCK. Every phase the bell, strand and fringe
       shaders see is timeS % CREATURE_T, and every rate on it is a whole
       number of cycles per CREATURE_T (k * 2pi / 60) so the wrap is seamless
       and fp32 phase math stays precise for days. The species GLSL sees
       CREATURE_T and FLUT_RATE as constants (SPECIES_HEAD) and the engine
       derives marginJS's flutPh from the same rate, so the JS roots and the
       GLSL rim flutter together by construction. */
    var CREATURE_T = 60;
    var FLUT_RATE = 40 * TWO_PI / CREATURE_T;   /* lappet flutter: the old 3D engine's 4.2 rad/s -> 4.18879 */
    var WOB_RATE = 15 * TWO_PI / CREATURE_T;    /* strand bead / ruffle wobble: the old 3D engine's 1.6 -> 1.5708 */
    var FRILL_RATE = 12 * TWO_PI / CREATURE_T;  /* frill travel: the brief's 1.3 -> 1.2566 */
    var SWAY_RATE = 8 * TWO_PI / CREATURE_T;    /* fringe sway: 0.8378 */
    var FRINGE_NODES = 6;            /* analytic nodes per fringe strand (a 12-vertex strip) */
    /* a frilled oral arm with at least this many columns across is drawn as
       a lace curtain (STRAND_VS; the spec's candidate B: "frill on, large
       amplitude, across >= 6"); fewer columns cannot carry a curled sheet
       with ruffled hems and keep the plain twist (candidate A's 4) */
    var LACE_ACROSS = 6;
    var STRAND_STRIDE = 11;          /* floats per strand vertex: pos3, tan3, side, u, width, bright, seed */
    /* the fringe strip template, (k along, side) per vertex — one for all instances */
    var fringeTpl = new Float32Array(FRINGE_NODES * 4);
    (function () {
      var k, o = 0;
      for (k = 0; k < FRINGE_NODES; k++) { fringeTpl[o++] = k; fringeTpl[o++] = -1; fringeTpl[o++] = k; fringeTpl[o++] = 1; }
    })();

    /* deterministic 0..1 hash for build-time jitter (JS doubles: the mediump
       trap that bans this form in shaders does not exist here) */
    function fr01(x) { var s = Math.sin(x) * 43758.5453; return s - Math.floor(s); }

    /* ------------------------------------------------------- quality tiers */
    /* The spec's tier table. scale is the scene target's render scale (the
       water is half of it, the rays a quarter); msaa 0 means FXAA in the
       composite; organ is the organ field's ray-march step count; the bell
       grid and the chain / fringe scales feed rebuildTierMeshes() (Task 5). */
    var TIERS = {
      ultra: { scale: 1.0, msaa: 4, bloom: 6, rays: 64, organ: 12, bellSeg: 256, bellRings: 128, chainScale: 1, fringeScale: 1 },
      high:  { scale: 0.85, msaa: 2, bloom: 5, rays: 40, organ: 8, bellSeg: 192, bellRings: 96, chainScale: 1, fringeScale: 0.75 },
      low:   { scale: 0.7, msaa: 0, bloom: 4, rays: 24, organ: 5, bellSeg: 128, bellRings: 64, chainScale: 0.6, fringeScale: 0.4 }
    };
    var ORDER = ['low', 'high', 'ultra'];
    /* stepping state: an EMA of the RAF-to-RAF wall time in ms, how long it
       has sat above 20 / below 11, and the time since the last change. That
       starts huge so the FIRST step needs no 8 s hold: a page that opens slow
       should not stay slow for 8 s to prove it. */
    var tier = 'ultra', ceiling = 'ultra', pinned = false;
    var ema = 16, slowFor = 0, fastFor = 0, sinceChange = 1e9, warm = 0;
    var WARM_FRAMES = 30;   /* frames ignored after a tier change: shader warm-up, not the tier's speed */

    /* Post tunables. The water alone never crosses the bloom threshold nor
       the ray luma floor: these become visible with the creature's light
       (Task 5) and are tuned there. */
    var BLOOM_AMT = 0.18, RAY_AMT = 0.55, RAY_DENSITY = 0.9;

    /* --------------------------------------------------------------- state */
    var timeS = 0, lastT = 0;
    var activity = 0, activityTarget = 0;
    var mouseNX = 0, mouseNY = 0, mouseIn = false, lastMouseT = -1e9, cursorA = 0;

    /* camera (ported from the 3D engine: same numbers) */
    var aspect = 1, tanY = Math.tan(FOVY / 2), tanXA = tanY;
    var eyeX = 0, eyeY = 0, eyeZ = 0, yaw = 0, pitch = 0;
    var rgt = new Float32Array(3), upv = new Float32Array(3), bck = new Float32Array(3);
    var urx = 0, ury = 0, urz = -1;   /* scratch unproject ray */
    var mProj = new Float32Array(16), mView = new Float32Array(16), mVP = new Float32Array(16);

    /* ---------------------------------------------------- the creature (hero) */
    /* ported from the 3D engine: the layout anchor, the spring that holds her
       to it under the swaying camera, the breath, the lean and the pulse
       responses keep their numbers. The shafts converge on her projected
       position (heroU/V), the god-ray light hangs above it, the motes rise
       around her. */
    var heroX = 0, heroY = 0, heroVX = 0, heroVY = 0;   /* anchor spring */
    var heroPX = 0, heroPY = 0;                         /* drawn position (with bob) */
    var heroScale = 2.2, heroFloor = 0, heroInited = false, heroMobile = false;
    var anchorPxX = 0, anchorPxY = 0;                   /* the layout anchor, CSS px */
    var pulsePhase = 0;              /* traveling contraction wave phase, 0..2pi */
    var leanX = 0, leanZ = 0;        /* bell lean (radians) */
    var flare = 0;                   /* pulse brightness flare */
    var kink = 0;                    /* post-poke tentacle zigzag energy */
    var waveRest = 0;                /* the breath phase at which the species' margin is at rest (widest) */
    var cTime = 0, flutPh = 0, wobPh = 0;   /* the bounded creature clock and its phases */
    var heroBright = 0.6, starTw = 0, starInt = 0;
    var shaftA = 0, shaftB = 0, shaftC = 0, shaftD = 0;      /* bounded shaft phases */
    var ripX = 0, ripY = 0, ripZ = 0, ripR = 0, ripA = 0;   /* mote click ripple */
    var heroU = 0.5, heroV = 0.5, heroR = 0.3, heroDk = 0;  /* backdrop contrast pocket */
    var m9 = new Float32Array(9);    /* bell model rot*scale, column-major */
    var m9i = new Float32Array(9);   /* its inverse (world dir -> bell local) */
    var mgOut = new Float64Array(3); /* root scratch handed to species.marginJS (zero allocation) */
    var rayDX = 0, rayDY = 0, rayDZ = -1;   /* pointer ray (world), for the stir */
    var hbX = 0, hbY = 0, hbW = 0, hbH = 0; /* info().heroBox, CSS px */

    /* verlet chains, strand streams and the bell grid: allocated ONCE per
       mount for the species' ULTRA counts by initCreature(); every tier
       fills a prefix (rebuildTierMeshes). Nothing here is allocated per
       frame. `groups` mirrors species.chains with the engine's derived
       numbers (kind 0 tentacle / 1 oral arm / 2 fringe, active count, the
       index range each group draws with). */
    var groups = [];
    var CH_MAX = 0, NODE_MAX = 0, VERT_MAX = 0, IDX_MAX = 0, FRINGE_MAX = 0;
    var chainsActive = 0, fringeActive = 0;
    var ndPos = null, ndPrev = null;
    var chOff = null, chLen = null, chPhi = null, chSeg = null, chKind = null, chSeed = null;
    var chGroup = null, chVert = null;
    var strandF = null, strandFloats = 0, strandIdx = null, strandIdxCount = 0;
    var fringeF = null;
    var bellV = null, bellI = null, bellIdxCount = 0;

    /* a11y text-column dim band (CSS px; converted to device px for the composite) */
    var dimL = 0, dimR = -1;

    /* the bounded period shared by the silhouettes and the motes below. It
       must be assigned before the silhouette block reads it. */
    var MOTE_T = 600;                /* shared mote period, seconds */

    /* distant silhouettes: static instance data (x,y,z,scale | rate,seed,alpha,0).
       The old engine fed these an UNBOUNDED u_time; here u_time is timeS %
       MOTE_T (600 s, shared with the motes) so the breathing rate is quantized
       to whole cycles per period and the wrap is seamless. */
    var silF = new Float32Array(N_SIL * 8);
    (function () {
      var d = [
        [-13.0, 2.5, -33, 1.10, 0.90, 1.3, 0.42],
        [-5.0, -3.0, -36, 0.85, 1.05, 4.1, 0.34],
        [3.0, 4.2, -31, 0.70, 1.15, 2.2, 0.38],
        [9.0, -1.5, -37, 1.25, 0.80, 5.0, 0.30],
        [16.0, 3.0, -34, 0.95, 1.00, 0.6, 0.36],
        [-20.0, -0.5, -38, 1.40, 0.75, 3.3, 0.28]
      ];
      var i, o;
      for (i = 0; i < N_SIL; i++) {
        o = i * 8;
        silF[o] = d[i][0]; silF[o + 1] = d[i][1]; silF[o + 2] = d[i][2];
        silF[o + 3] = d[i][3];
        silF[o + 4] = Math.round(d[i][4] * MOTE_T / TWO_PI) * TWO_PI / MOTE_T;
        silF[o + 5] = d[i][5]; silF[o + 6] = d[i][6];
        silF[o + 7] = 0;
      }
    })();

    /* ascending star-motes: instance data, preallocated once
       (ox, oy0, oz, size | rise rate, seed, twinkle rate, alpha | tag) —
       radius biased inward so the dust is dense near her, sparse far.
       The motes' u_time is bounded: uploaded as timeS % MOTE_T, so every
       rate is quantized at build time to whole cycles per MOTE_T (rise to
       k*span/T, twinkle to k*2pi/T; granularity ~0.01 — imperceptible) and
       the wrap at T is seamless. Keeps fp32 phase math precise forever.

       INSTANCE 0 IS HER NUCLEUS (tag 9; every mote is 0). It rides this same
       stream and this same draw. Slot 0 is the ONLY dynamic part of the
       buffer (36 bytes/frame). */
    var N_MOTE = 90;                /* buffer holds all; mobile draws fewer */
    var MOTE_STRIDE = 9;             /* floats per instance */
    var moteCount = N_MOTE + 1;
    var moteF = new Float32Array((N_MOTE + 1) * MOTE_STRIDE);
    var moteHead = new Float32Array(MOTE_STRIDE);   /* per-frame nucleus */
    (function () {
      function fr(x) { var s = Math.sin(x) * 43758.5453; return s - Math.floor(s); }
      var i, o;
      for (i = 0; i < N_MOTE; i++) {
        o = (i + 1) * MOTE_STRIDE;
        var rr = Math.pow(fr(i * 12.9898 + 1.3), 1.7) * 3.4 + 0.3;
        var aa = fr(i * 78.233 + 2.1) * TWO_PI;
        moteF[o] = Math.cos(aa) * rr;
        moteF[o + 1] = fr(i * 3.7 + 0.7) * 6.5;
        moteF[o + 2] = (fr(i * 9.1 + 4.2) - 0.5) * 2.4;
        moteF[o + 3] = 0.05 + 0.075 * fr(i * 5.3 + 3.3);
        moteF[o + 4] = Math.round((0.28 + 0.5 * fr(i * 7.7 + 0.2))
          * MOTE_T / 6.5) * 6.5 / MOTE_T;
        moteF[o + 5] = fr(i * 11.3 + 5.9) * TWO_PI;
        moteF[o + 6] = Math.round((1.4 + 2.6 * fr(i * 17.9 + 2.7))
          * MOTE_T / TWO_PI) * TWO_PI / MOTE_T;
        moteF[o + 7] = (0.30 + 0.45 * fr(i * 23.1 + 8.8)) * Math.max(0.25, 1.2 - 0.22 * rr);
        moteF[o + 8] = 0;             /* tag: 0 = dust, 9 = her nucleus */
      }
    })();

    /* shockwave shells (pulse), max 6 concurrent, slots reused. Task 5's
       pulse() claims slots; the sim and the ring draw are already here. */
    var shells = new Array(MAX_SHELL);
    var ji;
    for (ji = 0; ji < MAX_SHELL; ji++) {
      shells[ji] = { active: false, x: 0, y: 0, z: 0, r: 0, maxR: 0, speed: 0, delay: 0, strength: 0, alpha: 0 };
    }
    var shellF = new Float32Array(MAX_SHELL * 8);
    var shellCount = 0;
    var flash = { active: false, x: 0, y: 0, z: 0, t: 0, dur: 0.5 };

    /* GL objects: programs, static buffers and VAOs live in buildAll();
       every size-dependent target lives in sizeTargets() */
    var progBack = null, progSil = null, progMote = null, progShell = null, progGlow = null;
    var progBlit = null, progRays = null, progDown = null, progUp = null, progComp = null;
    var progBell = null, progStrand = null, progFringe = null;
    var triVao = null, silVao = null, moteVao = null, shellVao = null, glowVao = null;
    var bellVao = null, strandVao = null, fringeVao = null;
    var quadBuf = null, silGridBuf = null, silIdxBuf = null, silBuf = null, moteBuf = null, shellBuf = null;
    var bellVBuf = null, bellIBuf = null, strandBuf = null, strandIBuf = null, fringeBuf = null, fringeTplBuf = null;
    var silIdxCount = 0;
    var rsW = 0, rsH = 0;                    /* render-scale size */
    var waterTex = null, waterFbo = null, waterW = 0, waterH = 0;
    var sceneMs = null;                      /* {fbo, rb} or null without MSAA */
    var sceneTex = null, sceneFbo = null;
    var rayTex = null, rayFbo = null, rayW = 0, rayH = 0;
    var bloomTex = new Array(MAX_BLOOM), bloomFbo = new Array(MAX_BLOOM);
    var bloomW = new Int32Array(MAX_BLOOM), bloomH = new Int32Array(MAX_BLOOM);
    var bloomLevels = 0;
    var msaaSamples = 0;
    var preExp = 1, exposure = 1;            /* LDR: 0.25 into the targets, x4 out */
    var rayDecay = 0.96, rayWeight = 0, raySamples = 64;
    var wDimL = 0, wDimR = -1, wDimF = 1;    /* the dim band in the water target's pixels */
    var sized = [];                          /* what the next sizeTargets() must release */

    /* -------------------------------------------------------------- shaders */
    /* All GLSL ES 3.00 with highp fragments (ES 3.0 guarantees highp there),
       so the old mediump traps are gone. The no-sin hash stays: the spec
       mandates it, and it is the better-distributed hash anyway. */
    /* Samplers default to lowp in ES 3.00 and some mobile drivers take that
       literally: a half-float target read through a lowp sampler comes back
       at lowp range and precision, which is the HDR headroom gone. */
    var GLSL_HEAD = '#version 300 es\nprecision highp float;\nprecision highp sampler2D;\n';

    /* MEDIUMP-SAFE VALUE HASH, ported verbatim from the retired 3D engine's
       HASH_GLSL (only keywords change in GLSL 3.00; the body has none). No
       sin() (whose large-argument precision is driver-defined), no large
       constant, every intermediate under 40, so it holds even at mediump. */
    var HASH_GLSL = [
      'float vhash(vec2 p) {',
      '  vec2 g = fract(p * vec2(0.3819660, 0.6180340) + 0.11);',
      '  g = fract(vec2(g.x * (g.y * 8.0 + 1.7), g.y * (g.x * 8.0 + 1.7)));',
      '  return fract(g.x * 3.7 + g.y * 1.3);',
      '}'
    ].join('\n');

    /* DISPLAY -> LINEAR. The ported water layers were authored as
       display-referred colour written straight to the screen; they now feed
       a scene-linear HDR chain whose composite runs the tone curve. Each
       layer therefore computes its OLD display result and converts it to
       linear on the way out (the in-shader tonemaps are gone). That keeps
       their old look through the mid-tones and leaves everything above 1.0 as headroom for the
       creature's light, which is where bloom and the god rays come from. */
    var LIN_GLSL = 'vec3 lin(vec3 c) { return pow(max(c, vec3(0.0)), vec3(2.2)); }';

    /* THE A11Y DIM BAND, the retired 3D engine's rule (its DIM_GLSL) exactly:
       only the LIGHT layers behind the text column are held down —
       silhouettes, motes, glow, shells here and the creature, bloom and rays
       in the composite — never the backdrop (gradient, fog, shafts). Floor 0.35
       with a smoothstep feather. u_dim is (left, right, feather) in the
       pixels of the target being drawn (right < left disables), evaluated
       per fragment from gl_FragCoord: a quad can span the whole screen. */
    var DIM_GLSL = [
      'uniform vec3 u_dim;',
      'float dimBand(float px) {',
      '  if (u_dim.y < u_dim.x) { return 1.0; }',
      '  float t = 0.0;',
      '  if (px < u_dim.x) { t = (u_dim.x - px) / u_dim.z; }',
      '  else if (px > u_dim.y) { t = (px - u_dim.y) / u_dim.z; }',
      '  t = clamp(t, 0.0, 1.0);',
      '  t = t * t * (3.0 - 2.0 * t);',
      '  return 0.35 + 0.65 * t;',
      '}'
    ].join('\n');

    /* full-screen triangle from gl_VertexID: no buffer, shared by every pass */
    var TRI_VS = GLSL_HEAD + [
      'out vec2 v_uv;',
      'void main() {',
      '  vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);',
      '  v_uv = p;',
      '  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);',
      '}'
    ].join('\n');

    /* ---- WATER: backdrop (depth gradient, fog, converging shafts) ---- */
    var BACK_FS = GLSL_HEAD + [
      'in vec2 v_uv;',
      'out vec4 o;',
      /* shaft phases are precomputed sin() values uploaded per frame: never
         an unbounded time in the fragment stage */
      'uniform vec4 u_shaft;',
      'uniform float u_activity;',
      'uniform float u_aspect;',
      /* nature-documentary staging: a soft dark pocket of water directly
         behind the hero so the luminous creature pops off the frame */
      'uniform vec4 u_hero;',   /* uv.x, uv.y, radius (v units), strength */
      'uniform float u_pre;',   /* LDR pre-exposure (0.25); 1.0 in HDR */
      'uniform float u_dither;',/* 1 in LDR: this pass is the 8-bit gradient source */
      HASH_GLSL,
      LIN_GLSL,
      /* Ascension: every shaft is aimed at the hero — they spread apart at
         the surface and converge on her anchor (u_hero.xy), an annunciation
         column; the light dies just below her so she stands at its foot */
      'float shaft(vec2 uv, float off, float wBase, float sv) {',
      '  float hx = u_hero.x * u_aspect;',
      '  float dy = clamp((uv.y - u_hero.y) / max(1.08 - u_hero.y, 0.30), 0.0, 1.5);',
      '  float cx = hx + off * u_aspect * dy;',
      '  float w = wBase * (0.5 + dy * 2.0);',
      '  float d = abs(uv.x * u_aspect - cx) / max(w, 0.015);',
      '  float fall = max(0.0, 1.0 - d);',
      '  fall *= fall;',
      '  float fade = smoothstep(u_hero.y - 0.34, u_hero.y + 0.22, uv.y);',
      '  return fall * fade * (0.72 + 0.28 * sv);',
      '}',
      /* one code of the LDR water target (SRGB8) in linear units at value c:
         1/(255*12.92) on the sRGB curve's linear toe, else the inverted slope
         of 1.055*c^(1/2.4)-0.055. A flat 1/255 would be ~6 codes of noise in
         the dark, which is the speckle the sRGB storage is there to remove. */
      'vec3 srgbLsb(vec3 c) { return max(vec3(0.000304), 0.00892 * pow(max(c, vec3(1e-6)), vec3(0.5833))); }',
      'void main() {',
      '  vec3 bot = vec3(0.016, 0.039, 0.071);',   /* #040A12 */
      '  vec3 mid = vec3(0.031, 0.094, 0.169);',   /* #08182B */
      '  vec3 top = vec3(0.043, 0.125, 0.212);',   /* #0B2036 */
      '  vec3 col = mix(bot, mid, smoothstep(0.0, 0.62, v_uv.y));',
      '  col = mix(col, top, smoothstep(0.62, 1.0, v_uv.y));',
      /* two inner shafts land gold on her crown; two outer stay cool and
         faint — the warmth belongs to the light that FOUND her */
      '  float sg = 0.0;',
      '  sg += shaft(v_uv, 0.02, 0.055, u_shaft.x) * 0.215;',
      '  sg += shaft(v_uv, -0.13, 0.085, u_shaft.y) * 0.135;',
      '  float sc = 0.0;',
      '  sc += shaft(v_uv, 0.17, 0.120, u_shaft.z) * 0.085;',
      '  sc += shaft(v_uv, -0.31, 0.150, u_shaft.w) * 0.060;',
      /* the heavens surge while the scan runs */
      '  float surge = 1.0 + 0.55 * u_activity;',
      /* white-gold grade lives in the LIGHT only (never the flesh) */
      '  vec3 light = vec3(0.95, 0.87, 0.68) * (sg * surge);',
      '  light += vec3(0.55, 0.72, 0.90) * (sc * surge);',
      /* THE COLUMN: a hard narrow shaft standing on her crown and running
         clean to the surface — a nave, not a glow. She is at its foot. */
      '  float cw = abs((v_uv.x - u_hero.x) * u_aspect);',
      '  float cw1 = cw / 0.030, cw2 = cw / 0.105;',
      '  float colm = exp(-cw1 * cw1) * 0.85 + exp(-cw2 * cw2) * 0.30;',
      '  colm *= smoothstep(u_hero.y - 0.02, u_hero.y + 0.26, v_uv.y);',
      '  colm *= 0.55 + 0.45 * u_shaft.x;',
      '  light += vec3(0.98, 0.91, 0.74) * colm * 0.150 * surge;',
      /* the old 3D engine's semantics exactly: the backdrop — gradient, fog,
         shafts and the nave — is never dimmed; the a11y band holds down the light
         LAYERS drawn over it (silhouettes, motes, glow, shells), the creature
         in its own bell and strand stages, and bloom and rays in the
         composite */
      '  col += light;',
      '  col += vec3(0.5, 0.85, 1.0) * (u_activity * 0.05);',
      /* local darkening behind the hero (slightly elongated down the drape) */
      '  vec2 hd = vec2((v_uv.x - u_hero.x) * u_aspect, (v_uv.y - u_hero.y) * 0.8);',
      '  float hk = exp(-dot(hd, hd) / max(u_hero.z * u_hero.z, 1e-4));',
      '  col *= 1.0 - u_hero.w * hk;',
      /* radial falloff so the corners breathe dark, like the 2D backdrop */
      '  vec2 cc = vec2((v_uv.x - 0.5) * u_aspect, v_uv.y - 0.45);',
      '  col *= 1.0 - 0.35 * smoothstep(0.35, 1.15, length(cc));',
      '  col = lin(col) * u_pre;',
      /* a faint multiplicative texture in the water (this target is half
         resolution and bilinearly upsampled, so anything stronger here
         turns into 2x2 blotches); the old 3D engine's +-0.005 display-space
         grain is applied per DEVICE pixel in the composite instead, where
         that engine applied it. gl_FragCoord is folded into a 64px cell
         before hashing, as it was there. */
      '  float grain = vhash(mod(gl_FragCoord.xy, 64.0));',
      '  col *= 1.0 + (grain - 0.5) * 0.10;',
      /* LDR: the 8-bit water target would band on its own, so it is
         dithered at the source, not only in the composite — by one of ITS
         codes. STATIC, unlike the composite's: a source code is up to four
         screen codes after the exposure, and a per-frame pattern at that
         amplitude reads as TV static. */
      '  if (u_dither > 0.5) { col += (vhash(gl_FragCoord.xy) - 0.5) * srgbLsb(col); }',
      '  o = vec4(col, 0.0);',   /* alpha 0: coverage belongs to the creature */
      '}'
    ].join('\n');

    /* ---- WATER: distant silhouettes (static instance buffer; drift and
       breathing are functions of the bounded u_time — zero CPU per frame) ---- */
    var SIL_VS = GLSL_HEAD + [
      'in vec2 a_tp;',
      'in vec4 a_i0;',   /* x, y, z, scale */
      'in vec4 a_i1;',   /* rate (whole cycles per MOTE_T), seed, alpha, - */
      'uniform mat4 u_vp;',
      'uniform vec3 u_cam;',
      'uniform float u_time;',   /* timeS % MOTE_T */
      'uniform float u_fogD;',
      'out float v_a;',
      'void main() {',
      '  float t = a_tp.x;',
      '  float phi = a_tp.y;',
      '  float ph = u_time * a_i1.x + a_i1.y;',
      '  float s = sin(ph);',
      '  float ctr = max(s, 0.0); ctr *= ctr;',
      '  float th = t * 2.05;',
      '  float r = sin(th) * (1.0 - 0.20 * ctr);',
      '  float y = cos(th) * 0.60 * (1.0 + 0.12 * ctr);',
      '  vec3 p = vec3(r * cos(phi), y, r * sin(phi)) * a_i0.w;',
      /* drift rates 2pi*5/600 and 2pi*4/600: whole cycles per MOTE_T, so
         the u_time wrap is seamless (the old 3D engine's 0.05 and 0.04, quantized) */
      '  vec3 world = a_i0.xyz + p + vec3(sin(u_time * 0.05235988 + a_i1.y) * 1.8,',
      '    sin(u_time * 0.04188790 + a_i1.y * 1.7) * 2.4, 0.0);',
      '  gl_Position = u_vp * vec4(world, 1.0);',
      '  float dist = length(a_i0.xyz - u_cam);',
      '  v_a = a_i1.z * exp(-dist * u_fogD);',
      '}'
    ].join('\n');

    var SIL_FS = GLSL_HEAD + [
      'in float v_a;',
      'out vec4 o;',
      'uniform float u_pre;',
      LIN_GLSL,
      DIM_GLSL,
      'void main() {',
      '  o = vec4(lin(vec3(0.40, 0.35, 0.60) * v_a * dimBand(gl_FragCoord.x)) * u_pre, 0.0);',
      '}'
    ].join('\n');

    /* ---- WATER: the rising dust + her nucleus (one instanced draw) ----
       Plain gold/white star-dust drifting UP around the hero, dense near her,
       sparse far away; instance 0 is her own luminous NUCLEUS. Every fragment
       emits alpha 0, so the layer is purely additive light in the water.
       u_time is BOUNDED (timeS % MOTE_T) and every rate — rise, sway,
       twinkle — is a whole number of cycles per MOTE_T. */
    var MOTE_VS = GLSL_HEAD + [
      'in vec2 a_q;',
      'in vec4 a_i0;',   /* ox, oy0 (rise offset), oz, size */
      'in vec4 a_i1;',   /* rise rate, seed, twinkle rate, alpha */
      'in float a_tag;', /* 0 = dust, 9 = her nucleus */
      'uniform mat4 u_vp;',
      'uniform vec3 u_right;',
      'uniform vec3 u_up;',
      'uniform vec3 u_hpos;',
      'uniform float u_time;',
      'uniform float u_hs;',
      'uniform float u_act;',
      /* click ripple: brightness travels through nearby motes (xyz origin,
         w expanding radius; u_ripA amplitude — all bounded, CPU-driven) */
      'uniform vec4 u_rip;',
      'uniform float u_ripA;',
      'out vec2 v_q;',
      'out float v_a;',
      'out float v_tag;',
      'void main() {',
      '  v_tag = a_tag;',
      /* ---- INSTANCE 0: her nucleus. Placed straight in world space, no
         rise; tag 9 sends it down the nucleus branch in the fragment stage. */
      '  if (a_tag > 8.5) {',
      '    vec3 nw = a_i0.xyz + (u_right * a_q.x + u_up * a_q.y) * a_i0.w;',
      '    gl_Position = u_vp * vec4(nw, 1.0);',
      '    v_q = a_q;',
      '    v_a = a_i1.w;',
      '    return;',
      '  }',
      '  float span = 6.5;',
      '  float yy = mod(a_i0.y + u_time * a_i1.x, span);',
      '  float lu = yy / span;',
      /* she attracts the light: motes drift toward her axis as they rise */
      '  float cvg = 1.0 - 0.30 * lu;',
      '  vec3 base = u_hpos + vec3(a_i0.x * u_hs * cvg, yy - span * 0.42, a_i0.z * u_hs * cvg);',
      /* sway rate = 2pi*48/MOTE_T (48 whole cycles per 600 s period) so
         the u_time wrap at MOTE_T is seamless */
      '  base.x += sin(u_time * 0.50265482 + a_i1.y) * 0.14 * u_hs;',
      '  float tw = 0.35 + 0.65 * (0.5 + 0.5 * sin(u_time * a_i1.z + a_i1.y * 5.0));',
      '  tw *= tw;',
      /* fade in low, fade out high: no popping at the wrap seam */
      '  float lf = smoothstep(0.0, 0.12, lu) * (1.0 - smoothstep(0.80, 1.0, lu));',
      '  float rd = distance(base, u_rip.xyz);',
      /* squared by hand: the base is negative inside the ring and pow() of a
         negative base is undefined in GLSL ES 3.00 (see SPECIES_HEAD) */
      '  float rq = (rd - u_rip.w) * 0.7;',
      '  float rip = u_ripA * exp(-rq * rq);',
      '  vec3 world = base + (u_right * a_q.x + u_up * a_q.y) * (a_i0.w * u_hs);',
      '  gl_Position = u_vp * vec4(world, 1.0);',
      /* dust crossing her silhouette is held back so it reads as light in
         the water around her rather than speckle stuck to the jelly */
      '  float lat = length(base.xy - u_hpos.xy);',
      '  float inb = 1.0 - smoothstep(0.78 * u_hs, 1.16 * u_hs, lat);',
      '  float bhd = step(base.z, u_hpos.z);',
      '  v_a = a_i1.w * tw * lf * (0.78 + 0.44 * u_act) * (1.0 + 2.4 * rip)',
      '      * (1.0 - (0.30 + 0.30 * bhd) * inb);',
      '  v_q = a_q;',
      '}'
    ].join('\n');

    var MOTE_FS = GLSL_HEAD + [
      'in vec2 v_q;',
      'in float v_a;',
      'in float v_tag;',
      'out vec4 o;',
      'uniform float u_tw;',    /* bounded nucleus twinkle phase */
      'uniform float u_pre;',
      'uniform float u_floor;', /* the layout's dim floor (the old 3D engine's max(dimBand, u_floor)) */
      LIN_GLSL,
      DIM_GLSL,
      'void main() {',
      '  float r = length(v_q);',
      '  float dim = max(dimBand(gl_FragCoord.x), u_floor);',
      '  if (v_tag > 8.5) {',
      /* HER OWN LUMINOUS NUCLEUS: the gastric core seen glowing through the
         apex of the glass, with a soft 4-point sparkle. No headgear: this is
         the creature's own light. The old per-layer knee is gone — the
         composite's tone curve rolls the core off, gold, never to white. */
      '    float w1 = 0.85 + 0.15 * sin(u_tw);',
      '    float core = exp(-r * r * 90.0) * 1.15;',
      '    float rays = exp(-abs(v_q.x) * 30.0) * exp(-abs(v_q.y) * 4.5)',
      '               + exp(-abs(v_q.y) * 30.0) * exp(-abs(v_q.x) * 4.5);',
      '    rays *= w1 * smoothstep(1.0, 0.2, r) * 0.32;',
      '    float lr = r / 0.30;',
      '    float lantern = exp(-lr * lr) * 0.15 * (0.86 + 0.14 * sin(u_tw * 0.5));',
      '    float halo = exp(-r * r * 6.0) * 0.10;',
      '    float an = (core + rays + lantern + halo) * v_a * dim;',
      '    vec3 gold = vec3(1.00, 0.74, 0.26);',
      '    vec3 hot = vec3(1.00, 0.88, 0.56);',
      '    vec3 rad = mix(gold, hot, clamp(core, 0.0, 1.0) * 0.12) * an;',
      '    o = vec4(lin(rad) * u_pre, 0.0);',   /* alpha 0 -> pure additive */
      '    return;',
      '  }',
      /* PLAIN GOLD/WHITE STAR-DUST: a soft round core with a faint cross
         flare. Light in water, so alpha 0 — it never occludes anything. */
      '  float core = exp(-r * r * 9.0);',
      '  float cr = (exp(-abs(v_q.x) * 8.0) + exp(-abs(v_q.y) * 8.0)) * exp(-r * r * 2.2) * 0.30;',
      '  float ad = (core + cr) * v_a * dim;',
      '  o = vec4(lin(vec3(0.95, 0.89, 0.75) * ad) * u_pre, 0.0);',
      '}'
    ].join('\n');

    /* ---- WATER: sonar shells from pulse() (instanced billboards) ---- */
    var SHELL_VS = GLSL_HEAD + [
      'in vec2 a_q;',
      'in vec4 a_i0;',   /* x, y, z, radius */
      'in vec4 a_i1;',   /* alpha, -, -, - */
      'uniform mat4 u_vp;',
      'uniform vec3 u_right;',
      'uniform vec3 u_up;',
      'out vec2 v_q;',
      'out float v_a;',
      'void main() {',
      '  vec3 world = a_i0.xyz + (u_right * a_q.x + u_up * a_q.y) * a_i0.w;',
      '  gl_Position = u_vp * vec4(world, 1.0);',
      '  v_q = a_q;',
      '  v_a = a_i1.x;',
      '}'
    ].join('\n');

    var SHELL_FS = GLSL_HEAD + [
      'in vec2 v_q;',
      'in float v_a;',
      'out vec4 o;',
      'uniform float u_pre;',
      LIN_GLSL,
      DIM_GLSL,
      'void main() {',
      '  float r = length(v_q);',
      '  float band = max(0.0, 1.0 - abs(r - 0.82) / 0.14);',
      '  band *= band;',
      '  float dim = dimBand(gl_FragCoord.x);',
      /* the ring core stays CYAN, not near-white: held at (95,203,238) it
         reads as sonar rather than as a flashbulb; under the dim band the
         core is also pulled back toward the base cyan (the 3D engine's rule) */
      '  vec3 col = mix(vec3(0.0, 0.643, 0.863), vec3(0.72, 0.94, 1.0), band * 0.52 * dim);',
      '  o = vec4(lin(col * band * v_a * dim) * u_pre, 0.0);',
      '}'
    ].join('\n');

    /* ---- WATER: soft glow billboard (cursor light, sonar flash) ---- */
    var GLOW_VS = GLSL_HEAD + [
      'in vec2 a_q;',
      'uniform mat4 u_vp;',
      'uniform vec3 u_pos;',
      'uniform vec3 u_right;',
      'uniform vec3 u_up;',
      'uniform float u_size;',
      'out vec2 v_q;',
      'void main() {',
      '  vec3 world = u_pos + (u_right * a_q.x + u_up * a_q.y) * u_size;',
      '  gl_Position = u_vp * vec4(world, 1.0);',
      '  v_q = a_q;',
      '}'
    ].join('\n');

    var GLOW_FS = GLSL_HEAD + [
      'in vec2 v_q;',
      'out vec4 o;',
      'uniform vec3 u_color;',
      'uniform float u_int;',
      'uniform float u_pre;',
      'uniform float u_floor;',
      LIN_GLSL,
      DIM_GLSL,
      'void main() {',
      '  float r = length(v_q);',
      '  float a = exp(-r * r * 4.0) * smoothstep(1.0, 0.65, r) * u_int;',
      '  a *= max(dimBand(gl_FragCoord.x), u_floor);',
      '  o = vec4(lin(u_color * a) * u_pre, 0.0);',
      '}'
    ].join('\n');

    /* ================================================== THE CREATURE ====
       Everything a species' bell GLSL may rely on, declared before it in both
       stages: the bounded creature clock, the flutter rate the engine also
       drives marginJS with, the shared hash / value noise, sq() and fw(), and
       the organ march's footprint and contract (below).
       Two contract fields on the JS side shape what the engine draws around
       this GLSL (spec, "The species contract"):
       - apexY: the bell-local height of the apex star — her nucleus, drawn
         in the water behind the glass on the bell's up axis. It sits at the
         apex at rest plus the 0.06 crown lift both species share, so the
         star rides just over the crown and never sinks into it (A 0.60 ->
         0.66, B 0.90 -> 0.96).
       - a frilled oral arm with frill.across >= LACE_ACROSS is drawn as a
         LACE CURTAIN (STRAND_VS): a sheet curled about the arm's axis whose
         arc is the chain's width, spiralling down the arm, with frill.amp
         ruffling its hems off the sheet's own normal at frill.freq; a
         narrower frilled ribbon keeps the plain twist. */
    var SPECIES_HEAD = [
      'const float CREATURE_T = 60.0;',
      'const float FLUT_RATE = 4.1887902;',   /* 40 cycles per CREATURE_T: the old 3D engine's 4.2 rad/s, quantised */
      /* pow(x, y) is UNDEFINED for x < 0 in GLSL ES 3.00 (section 8.2): a
         driver that implements it as exp2(y * log2(x)) returns NaN, which a
         float target keeps and the bloom then spreads over the frame.
         SwiftShader hides it by strength-reducing a literal 2.0, so no e2e
         frame can catch it. Squares use sq(); every other pow() base in this
         engine and in the species files is wrapped in max / abs / clamp, and
         test_jellyfield_glsl_static.py fails any file that breaks that. */
      'float sq(float x) { return x * x; }',
      HASH_GLSL,
      'float vnoise(vec2 p) {',
      '  vec2 i = floor(p); vec2 f = fract(p);',
      '  f = f * f * (3.0 - 2.0 * f);',
      '  return mix(mix(vhash(i), vhash(i + vec2(1.0, 0.0)), f.x),',
      '             mix(vhash(i + vec2(0.0, 1.0)), vhash(i + vec2(1.0, 1.0)), f.x), f.y);',
      '}',
      /* THE ORGAN MARCH'S FOOTPRINT, set by the engine before each march
         (bell fragment stage only; the vertex stage sees the defaults):
         organDir  — the march direction, bell-local, unit;
         organStep — the step length, bell-local (each organField sample
                     stands for the segment +-organStep/2 around p);
         organPx   — bell-local length of one screen pixel at this fragment.
         A species may ignore them (a plain field is sampled as before) or
         use them to integrate thin anatomy across the step analytically —
         a canal one pixel wide that a 12-step march would otherwise hit or
         miss per pixel — and to fade detail finer than a pixel. That is how
         organs stay crisp and alias-free at any resolution and any tier.
         organEntry — the bell-local point the march starts from: the near
                     wall's surface point at this fragment (the interpolated
                     bellShape position).
         THE MARCH CONTRACT (a species may rely on exactly this; a static
         test, test_jellyfield_glsl_static.py, pins it):
         - organField is called from ONE place: the near-wall march in the
           bell fragment stage. The far wall, the vertex stages and the
           fringe never call it.
         - Per near-wall fragment the engine sets organDir, organStep,
           organPx and organEntry, then calls organField(p, time) for
           k = 0, 1, ... u_organSteps - 1 IN ORDER, from the wall inward, at
           p = organEntry + organDir * organStep * (k + 0.5): the midpoint of
           the k-th segment. So dot(p - organEntry, organDir) is the depth
           of the sample along the march, and the first call is the one with
           depth organStep / 2.
         - Each call's rgb is integrated as rgb x organStep x the
           transmittance so far; its a then attenuates everything after it:
           T *= exp(-a x organStep). A species that returns E / organStep at
           the first sample alone adds exactly E, at every tier.
         organEntry is what lets a species bind pattern to the SURFACE (B's
         stripes, pigment in the skin) at the exact surface point and once
         per fragment, instead of reading it at interior midpoints, where a
         radial pattern lies at a different angle on every step. */
      'vec3 organDir = vec3(0.0, -1.0, 0.0);',
      'float organStep = 0.1;',
      'float organPx = 0.005;',
      'vec3 organEntry = vec3(0.0);'
    ].join('\n');
    /* the species source is compiled into BOTH stages (the vertex stage
       needs bellShape) and fwidth() exists only in the fragment stage, so
       the species antialias through fw(): the screen-space width there, and
       0 where there is no screen space */
    var SPECIES_HEAD_VS = SPECIES_HEAD + '\nfloat fw(float x) { return 0.0; }';
    var SPECIES_HEAD_FS = SPECIES_HEAD + '\nfloat fw(float x) { return fwidth(x); }';

    /* ---- SCENE: the bell. The program is BELL_VS_HEAD + species.bell.glsl
       + BELL_VS_MAIN (and the FS likewise): the species defines bellShape,
       bellThickness, bellSurface and organField; the engine owns the
       geometry, the refraction, the organ march and the lighting model.
       The vertex stage generates the surface from (t, phi) so the traveling
       contraction produces CORRECT normals (finite differences of the
       deformed surface), as the old 3D engine's BELL_VS did. */
    var BELL_VS_HEAD = GLSL_HEAD + [
      'in vec2 a_tp;',
      'uniform mat4 u_vp;',
      'uniform vec3 u_pos;',
      'uniform mat3 u_m;',
      'uniform float u_wave;',    /* breath phase 0..1 */
      'uniform float u_time;',    /* timeS % CREATURE_T */
      'out vec2 v_tp;',
      'out vec3 v_n;',
      'out vec3 v_wp;',
      'out vec3 v_lp;',
      SPECIES_HEAD_VS
    ].join('\n') + '\n';
    var BELL_VS_MAIN = [
      'void main() {',
      '  float t = a_tp.x;',
      '  float phi = a_tp.y;',
      '  vec3 p0 = bellShape(t, phi, u_wave, u_time);',
      '  float tn = max(t, 0.02);',              /* apex: r=0 kills the phi tangent */
      '  vec3 q0 = bellShape(tn, phi, u_wave, u_time);',
      '  vec3 qT = bellShape(tn + 0.012, phi, u_wave, u_time);',
      '  vec3 qP = bellShape(tn, phi + 0.02, u_wave, u_time);',
      '  vec3 nl = cross(qP - q0, qT - q0);',    /* outward: d/dphi x d/dt */
      '  vec3 world = u_pos + u_m * p0;',
      '  gl_Position = u_vp * vec4(world, 1.0);',
      '  v_n = normalize(u_m * nl);',            /* u_m is rotation x uniform scale: directions survive it */
      '  v_wp = world;',
      '  v_lp = p0;',
      '  v_tp = a_tp;',
      '}'
    ].join('\n');

    var BELL_FS_HEAD = GLSL_HEAD + [
      'in vec2 v_tp;',
      'in vec3 v_n;',
      'in vec3 v_wp;',
      'in vec3 v_lp;',
      'out vec4 o;',
      'uniform sampler2D u_water;',   /* the half-res water target: what the glass refracts (never sceneTex: it is being drawn) */
      'uniform vec2 u_vpSize;',       /* the scene target's size: gl_FragCoord -> its uv, which is the water's uv too */
      'uniform vec3 u_cam;',
      'uniform vec3 u_viewR;',        /* camera right / up: the view-space normal for the refraction offset */
      'uniform vec3 u_viewU;',
      'uniform mat3 u_mi;',           /* inverse bell model: world dir -> bell local (the organ march) */
      'uniform float u_wave;',
      'uniform float u_time;',
      'uniform int u_organSteps;',    /* 12 / 8 / 5 by tier */
      'uniform float u_glow;',        /* heroBright: fog, activity and the pulse flare */
      'uniform float u_pre;',         /* LDR pre-exposure on the emitted light (the water sample already carries it) */
      'uniform float u_cov;',         /* base coverage of this wall: 0.62 near, 0.45 far (Fresnel and gold raise it) */
      'uniform float u_wall;',        /* 1 = the near wall (marches the organs), 0 = the far wall */
      'uniform vec3 u_gold;',         /* palette.gold, linear HDR */
      'uniform vec3 u_rim;',          /* palette.rim: the mesoglea's light at grazing incidence */
      'uniform vec3 u_body;',         /* palette.body: light scattered inside the crown */
      'uniform float u_floor;',       /* the layout's dim floor for the creature (0.55 phone band, 0.5 behind glass) */
      DIM_GLSL,                       /* u_dim in the scene target's pixels */
      SPECIES_HEAD_FS
    ].join('\n') + '\n';
    var BELL_FS_MAIN = [
      'void main() {',
      '  float t = v_tp.x;',
      '  float phi = v_tp.y;',
      '  vec3 n = normalize(v_n);',
      '  vec3 v = normalize(v_wp - u_cam);',
      '  if (dot(n, v) > 0.0) { n = -n; }',     /* shade the wall the eye sees */
      '  float ndv = clamp(-dot(n, v), 0.0, 1.0);',
      '  float F = 0.04 + 0.96 * pow(max(1.0 - ndv, 0.0), 5.0);',
      /* screen-space refraction of the water: the view-space normal times the
         mesoglea thickness, three taps for the dispersion (R short, B long) */
      '  float th = bellThickness(t, phi);',
      '  vec2 nv = vec2(dot(n, u_viewR), dot(n, u_viewU));',
      '  vec2 uv = gl_FragCoord.xy / u_vpSize;',
      '  vec2 off = nv * th * 0.06;',
      '  vec3 refr = vec3(texture(u_water, uv + off * 0.98).r,',
      '                   texture(u_water, uv + off).g,',
      '                   texture(u_water, uv + off * 1.02).b);',
      /* organs marched INSIDE the body, from the NEAR wall only: along the
         refracted ray (bell-local), u_organSteps steps of thickness*1.8/steps
         sampled at the segment midpoints, each adding emission x
         transmittance x its length (so 5 steps and 12 sum alike) while the
         transmittance also dims the water seen behind them: true parallax
         and depth, the organs sit IN the jelly. The far wall used to march
         too — outward, out of the body, doubling the organs and glowing
         where nothing is — so it now only shades its glass. The species
         sees the step and its footprint through the SPECIES_HEAD globals,
         and this loop is THE MARCH CONTRACT documented there (one call
         site, in order, first midpoint half a step in from organEntry). */
      '  organDir = normalize(u_mi * refract(v, n, 1.0 / 1.02));',
      '  organStep = th * 1.8 / float(u_organSteps);',
      '  organPx = length(fwidth(v_lp));',        /* outside the loop: derivatives want uniform flow */
      '  organEntry = v_lp;',
      '  vec3 organs = vec3(0.0);',
      '  float T = 1.0;',
      '  if (u_wall > 0.5) {',
      '    vec3 p = organEntry + organDir * (organStep * 0.5);',
      '    for (int i = 0; i < 12; i++) {',
      '      if (i >= u_organSteps) break;',
      '      vec4 f = organField(p, u_time);',
      '      organs += f.rgb * T * organStep;',
      '      T *= exp(-f.a * organStep);',
      '      p += organDir * organStep;',
      '    }',
      '  }',
      /* the surface: the species' tint and gold mask; the key light's wet
         gleam (pow 180); a metallic gleam on the gold (Blinn-Phong pow 96 in
         palette.gold); and the surface light's reflection, brightest where
         the glass mirrors the sky */
      '  vec4 surf = bellSurface(t, phi, n, u_time);',
      '  vec3 L = vec3(0.284034, 0.946779, 0.151485);',
      '  vec3 h = normalize(L - v);',
      '  float ndh = max(dot(n, h), 0.0);',
      '  float dl = max(dot(n, L), 0.0);',
      '  vec3 gold = u_gold * surf.a * (0.25 + 0.75 * dl + pow(max(ndh, 0.0), 96.0) * 2.5);',
      '  float spec = pow(max(ndh, 0.0), 180.0) * 0.8;',
      '  vec3 r = reflect(v, n);',
      '  vec3 refl = vec3(0.70, 0.82, 1.0) * (0.15 + 0.85 * pow(max(r.y, 0.0), 3.0)) * 0.9;',
      /* THE LIGHT IN THE GLASS. Two terms the water sample cannot give
         (it is dark): the rim — the mesoglea catches the shafts along the
         silhouette, brightest where the light lands on the crown and fading
         down the sides (palette.rim) — and the body — light scattered inside
         the crown, thick where the mesoglea is thick and where the key light
         hits (palette.body). Without them the glass reads as smoked plastic. */
      /* a soft halo, brightest at the silhouette and fading slowly inward
         (pow 2), so the edge is one clean step and its inner side never
         reads as a second edge — a thin pow-5 line here was ~2 px wide at
         1x and its inner edge merged with the step, widening the judge's
         rim profile past 2 px (the Fresnel mirror term above already gives
         the sharp grazing gleam); squared in the light so the crown's rim
         glows and the flanks stay dark (the header text sits beside them on
         the phone) */
      '  float g1 = max(1.0 - ndv, 0.0);',
      '  float grz = g1 * g1;',
      '  vec3 rim = u_rim * grz * (0.06 + 0.94 * dl * dl) * 0.85;',
      '  vec3 sss = u_body * th * (0.20 + 0.80 * dl) * 0.16;',
      '  float lit = u_glow * u_pre;',
      '  vec3 glass = refr * (1.0 - F) * surf.rgb * T;',
      '  vec3 emis = (organs + rim + sss + spec + gold) * lit * mix(0.45, 1.0, u_wall);',
      '  vec3 rgb = mix(glass, refl * lit, F);',
      /* coverage: the base per wall, raised toward 1 by the Fresnel mirror
         at the rim and by the gold (metal occludes); the emission is NOT
         scaled by it — it is the wall's own light, and the far wall's shows
         through the near wall at 1 - its coverage, which is what makes the
         bell read as a volume rather than a dome */
      '  float a = clamp(u_cov + (1.0 - u_cov) * max(F * 0.6, surf.a * 0.7), 0.0, 1.0);',
      /* THE A11Y DIM, the 3D engine's rule exactly (its col *= v_dim): the
         creature's own colour is held down to max(band, floor) behind the
         text column — display-space factors, so converted with pow 2.2 for
         these linear values — while the water it lets through (1 - a) is
         never dimmed. It lives here, not in the composite, because the
         composite can only dim by coverage, and a translucent wall's light
         (coverage 0.45-0.62) would then dim by half its due. */
      '  float dim = pow(max(dimBand(gl_FragCoord.x), u_floor), 2.2);',
      '  o = vec4((rgb * a + emis) * dim, a);', /* premultiplied over: the scene alpha becomes her coverage */
      '}'
    ].join('\n');

    /* camera-facing expansion in DEVICE PIXELS, shared by the strands and
       the fringe: the strip is never thinner than 1 px, and `cov` is the
       true-width / drawn-width ratio the fragment stage scales alpha by, so
       a hair-fine strand stays crisp and alias-free at any DPR. Needs u_vp,
       u_vpSize and u_pxPerUnit declared before it. */
    var EXPAND_GLSL = [
      'vec4 expand(vec3 p, vec3 tn, float side, float w, out float cov, out float halfOut) {',
      '  vec4 c0 = u_vp * vec4(p, 1.0);',
      '  float tl = length(tn);',
      '  tn = tl > 1e-6 ? tn / tl : vec3(0.0, -1.0, 0.0);',
      '  vec4 c1 = u_vp * vec4(p + tn * 0.05, 1.0);',
      '  vec2 d2 = (c1.xy / max(c1.w, 1e-4) - c0.xy / max(c0.w, 1e-4)) * u_vpSize;',
      '  float dl = length(d2);',
      '  d2 = dl > 1e-5 ? d2 / dl : vec2(0.0, 1.0);',
      '  float halfPx = w * 0.5 * u_pxPerUnit / max(c0.w, 1e-4);',
      '  float drawHalf = max(halfPx, 0.5);',    /* never thinner than 1 px */
      '  c0.xy += vec2(-d2.y, d2.x) * (side * drawHalf) * (2.0 / u_vpSize) * c0.w;',
      '  cov = halfPx / drawHalf;',
      '  halfOut = drawHalf;',
      '  return c0;',
      '}'
    ].join('\n');

    /* ---- SCENE: verlet strands — tentacles (filaments) and oral arms
       (ribbons with `across` columns, frilled). One node record per vertex,
       streamed once per frame from the preallocated strandF. ---- */
    var STRAND_VS = GLSL_HEAD + [
      'in vec3 a_pos;',
      'in vec3 a_tan;',
      'in vec4 a_aux;',     /* side -1..1, u along, full width (world), brightness */
      'in float a_seed;',   /* per-chain seed */
      'uniform mat4 u_vp;',
      'uniform vec2 u_vpSize;',
      'uniform float u_pxPerUnit;',   /* device px per world unit at clip w = 1 */
      'uniform vec3 u_cam;',
      'uniform vec3 u_frill;',        /* amp (world), freq (per bell-local unit along), enable */
      'uniform float u_len;',         /* the chain's bell-local length: freq x s with s = u * len */
      'uniform float u_time;',
      'uniform float u_lace;',        /* 1: this ribbon is a lace curtain (see below) */
      'out vec2 v_su;',
      'out float v_cov;',
      'out float v_halfPx;',
      'out float v_br;',
      'out float v_op;',
      'out float v_seed;',
      'out vec3 v_n;',
      'out vec3 v_v;',
      EXPAND_GLSL,
      'void main() {',
      '  float side = a_aux.x;',
      '  float u = a_aux.y;',
      '  vec3 p = a_pos;',
      '  v_op = 1.0;',
      '  vec3 v = normalize(p - u_cam);',
      '  vec3 tn = a_tan;',
      '  float tl = length(tn);',
      '  tn = tl > 1e-6 ? tn / tl : vec3(0.0, -1.0, 0.0);',
      '  vec3 across = cross(tn, v);',
      '  float al = length(across);',
      '  across = al > 1e-5 ? across / al : vec3(1.0, 0.0, 0.0);',
      '  vec3 nrm = cross(across, tn);',
      '  float cov, hp;',
      /* THE LACE CURTAIN: a frilled ribbon with >= LACE_ACROSS (6) columns
         (the spec's candidate-B arms: "frill on, large amplitude, across >= 6"; a
         narrower ribbon has no interior columns to carry it and keeps the
         plain twist below, unchanged). A flat strip whose edges swing in
         and out draws hard diamonds; a curtain is a curved SHEET:
         - its cross-section is an arc, curled LACE_CURL (1.15 rad) either
           side of the arm's axis, so even edge-on it shows a sliver of
           sheet and never pinches to a point;
         - the arc turns about the axis down the arm and slowly over time
           (th: 2.1 rad per bell-local unit, 3 turns per CREATURE_T), which
           is the spiral, and its width in the picture breathes with it;
         - its outer columns (|side|^3) ruffle OFF the sheet along the
           sheet's own normal, amp x sin(freq x s + time) as the spec says,
           each hem on its own phase, and the shading normal tilts with the
           ruffle's slope;
         and it is projected directly: a curtain is tens of pixels wide, so
         the 1-px expansion is not needed (MSAA and the fragment stage's
         hem fade antialias it). */
      '  if (u_lace > 0.5) {',
      '    float s = u * u_len;',
      '    float th = s * 2.1 - u_time * 0.3141593 + a_seed;',
      '    float ca = cos(th + side * 1.15), sa = sin(th + side * 1.15);',
      '    vec2 xs = (a_aux.z * 0.5 / 1.15) * vec2(sa - sin(th), ca - cos(th));',
      '    float hem = side * side * abs(side);',
      '    float rph = u_frill.y * s + u_time * ' + FRILL_RATE.toFixed(7) + ' + a_seed * 1.7 + (side < 0.0 ? 2.3 : 0.0);',
      '    xs += vec2(sa, ca) * (u_frill.x * sin(rph) * hem);',
      '    p += across * xs.x + nrm * xs.y;',
      '    nrm = normalize(across * sa + nrm * ca - tn * (0.9 * cos(rph) * hem));',
      '    gl_Position = u_vp * vec4(p, 1.0);',
      '    cov = 1.0; hp = 1e3;',
      '  } else {',
      /* frill: the edge columns ruffle SIDEWAYS (visible against the water)
         with a smaller out-of-plane fold that tilts the shading normal; the
         two edges run in opposite phase so the ribbon twists rather than
         bends. amp x sin(freq x s + time x 1.3 + seed), as the brief says. */
      '    if (u_frill.z > 0.5) {',
      '      float ph = u_frill.y * u * u_len + u_time * ' + FRILL_RATE.toFixed(7) + ' + a_seed;',
      '      float d = u_frill.x * sin(ph) * side * abs(side);',
      '      p += across * d + nrm * (0.5 * d);',
      '      nrm = normalize(nrm + across * (cos(ph) * side * 0.8));',
      '    }',
      '    gl_Position = expand(p, tn, side, a_aux.z, cov, hp);',
      '  }',
      '  v_su = vec2(side, u);',
      '  v_cov = cov;',
      '  v_halfPx = hp;',
      '  v_br = a_aux.w;',
      '  v_seed = a_seed;',
      '  v_n = nrm;',
      '  v_v = v;',
      '}'
    ].join('\n');

    /* the strand fragment stage, shared with the fringe: analytic 1-px edge
       fade, the coverage ratio, then a filament (soft round core + wet
       thread) or a two-sided translucent ribbon (wrap-lit by the key light,
       back-lit by the shafts, lace folds travelling down it) */
    var STRAND_FS = GLSL_HEAD + [
      'in vec2 v_su;',
      'in float v_cov;',
      'in float v_halfPx;',
      'in float v_br;',
      'in float v_op;',           /* opacity: a faint strand is also a translucent one */
      'in float v_seed;',
      'in vec3 v_n;',
      'in vec3 v_v;',
      'out vec4 o;',
      'uniform vec3 u_col;',      /* the chain group's linear colour */
      'uniform float u_kind;',    /* 0 filament, 1 ribbon */
      'uniform float u_pre;',
      'uniform float u_wob;',     /* bounded bead / fold phase */
      'uniform float u_ferrule;', /* the chain group's goldFerrules flag */
      'uniform vec3 u_gold;',     /* palette.gold, linear HDR */
      'uniform float u_floor;',   /* the layout's dim floor for the creature */
      /* the lace curtain's (shared with the vertex stage; the fringe program
         leaves u_lace at 0 and never reads the others) */
      'uniform float u_lace;',
      'uniform vec3 u_frill;',
      'uniform float u_len;',
      'uniform float u_time;',
      DIM_GLSL,                   /* u_dim in the scene target's pixels */
      'float sq(float x) { return x * x; }',
      'void main() {',
      /* MSAA evaluates the fragment at the pixel CENTRE, which can lie
         outside a 1-px strip, so the interpolated side extrapolates past
         +-1 there; unclamped, prof goes negative and pow(prof, 8.0) is NaN —
         a field of black and white squares on any float target. Clamp it. */
      '  float side = clamp(v_su.x, -1.0, 1.0);',
      '  float u = v_su.y;',
      /* pixel-centre coverage of the strip's edge, 1 px wide */
      '  float edge = clamp((1.0 - abs(side)) * v_halfPx + 0.5, 0.0, 1.0);',
      '  float prof = 1.0 - side * side;',
      '  float a;',
      '  vec3 col;',
      '  if (u_kind < 0.5) {',
      /* the root does not fade — it grows out of the mantle; the tip thins
         to water; nematocyst beads swell down the strand (integer u_wob
         multiplier: the phase wraps at 2pi). Gentler than the old 3D
         engine's 0.30: on a 1-px filament a deep bead modulation reads as a
         dashed line. */
      '    float bead = 0.90 + 0.12 * sin(u * 46.0 - u_wob * 2.0 + v_seed);',
      '    a = (prof * 0.55 + pow(max(prof, 0.0), 8.0) * 0.45) * (1.0 - 0.62 * u) * bead;',
      '    a *= smoothstep(0.0, 0.05, u);',
      '    col = u_col * (0.55 + 0.45 * prof) + vec3(0.30, 0.34, 0.46) * pow(max(prof, 0.0), 6.0);',
      '  } else if (u_lace > 0.5) {',
      /* THE LACE CURTAIN (see the vertex stage), two-sided translucent:
         - a sheet seen obliquely holds more tissue per pixel, so where the
           spiral turns it edge-on it reads as a brighter fold line (dens);
         - the hems are denser and brighter than the sheet (hemA), and fine
           pleats — three per frill wave, travelling with it, broad soft
           crests running in from the hem (a narrow crest reads as a string
           of beads) — are shaded per PIXEL (no tessellation needed), faded
           out before they could alias (their phase change per pixel);
         - lit through from either face by the surface light (|n.L|), and
           brightest high up, under the bell in the light column;
         - the outermost 14% of the hem thins to 30% (soft), and the edge
           itself fades over 1.6 px (fwidth(side)): soft edges at any DPR,
           wherever the sheet folds over itself;
         - it grows out of the oral disc and thins away at the tip. */
      '    vec3 L = vec3(0.284034, 0.946779, 0.151485);',
      '    float nl = length(v_n);',
      '    vec3 nn = nl > 1e-5 ? v_n / nl : vec3(0.0, 0.0, 1.0);',
      '    float fz = abs(dot(nn, v_v));',
      '    float as = abs(side);',
      '    float hemA = smoothstep(0.40, 0.88, as);',
      '    float sl = u * u_len;',
      '    float rq = 0.5 + 0.5 * sin(u_frill.y * 3.0 * sl + u_time * ' + FRILL_RATE.toFixed(7) + ' + v_seed * 2.9 + (side < 0.0 ? 1.1 : 0.0));',
      '    float crest = sq(rq) * (1.0 - smoothstep(0.8, 1.6, fwidth(sl) * u_frill.y * 3.0));',
      '    float dens = min(1.0 / (0.28 + 0.72 * fz), 2.6);',
      '    float edgeA = clamp((1.0 - as) / max(fwidth(side) * 1.6, 1e-4), 0.0, 1.0);',
      '    float soft = 1.0 - 0.70 * smoothstep(0.86, 1.0, as);',
      '    float env = smoothstep(0.0, 0.07, u) * (1.0 - smoothstep(0.66, 1.0, u));',
      '    float lace = 0.88 + 0.45 * crest * smoothstep(0.15, 0.90, as);',
      '    a = (0.13 + 0.17 * hemA) * dens * lace * env * edgeA * soft;',
      '    float lit = 0.40 + 0.60 * abs(dot(nn, L));',
      '    float trn = sq(max(dot(v_v, -L), 0.0));',
      '    col = u_col * (lit * (0.80 + 0.20 * lace) + 0.25 * hemA) * (1.15 - 0.45 * u) + u_col * trn * 0.5;',
      '  } else {',
      '    vec3 L = vec3(0.284034, 0.946779, 0.151485);',
      /* a twisting ribbon interpolates opposing normals through zero:
         normalize(0) is NaN, so the length is checked first */
      '    float nl = length(v_n);',
      '    vec3 nn = nl > 1e-5 ? v_n / nl : vec3(0.0, 0.0, 1.0);',
      '    float wrap = 0.5 + 0.5 * dot(nn, L);',
      '    float trn = pow(max(dot(v_v, -L), 0.0), 2.0);',
      '    float fold = 0.5 + 0.5 * sin(u * 21.0 + u_wob + v_seed);',
      '    a = prof * (0.30 + 0.35 * fold) * (1.0 - 0.45 * u)',
      '      * smoothstep(0.0, 0.12, u) * (1.0 - smoothstep(0.72, 1.0, u)) * 0.75;',
      '    col = u_col * (0.35 + 0.65 * wrap) + u_col * trn * 0.6;',
      '  }',
      '  a *= edge * v_cov * v_op;',
      /* GOLD FERRULES (contract: chains[].goldFerrules). Rings of metal
         clasping the strand near its root: one collar band on every strand
         and, on every fourth, a short course of three more opening out down
         its length — the classic's hierarchy, goldsmithing rather than
         glitter. The band is shaded as a little cylinder (bright crest, dark
         edges) in palette.gold and is its own radiance over the tissue, so
         it reads as solid metal even on a hair-fine strand. */
      '  float af = 0.0;',
      '  vec3 orn = vec3(0.0);',
      '  if (u_ferrule > 0.5) {',
      '    float ferr;',
      '    if (u_kind < 0.5) {',
      '      float cid = floor(v_seed / 2.399 + 0.5);',
      '      float course = mod(cid, 4.0) < 0.5 ? 1.0 : 0.0;',
      '      ferr = exp(-sq((u - 0.050) / 0.010)) * 0.70',
      '           + course * (exp(-sq((u - 0.130) / 0.0095)) * 0.78',
      '                     + exp(-sq((u - 0.240) / 0.0085)) * 0.50',
      '                     + exp(-sq((u - 0.390) / 0.0075)) * 0.25);',
      '    } else {',
      /* a ribbon is wide: two narrow clasps at the root, nothing more, or
         the arm wears a belt of gold rings */
      '      ferr = exp(-sq((u - 0.045) / 0.0055)) * 0.55 + exp(-sq((u - 0.105) / 0.0040)) * 0.18;',
      '    }',
      '    float crest = pow(max(prof, 0.0), 3.0);',
      '    af = min(ferr, 1.0) * edge * v_cov;',
      '    orn = u_gold * (0.30 + 0.70 * crest) * af;',
      '  }',
      /* the a11y dim, the old 3D engine's rule: the strand's own light is
         held down to max(band, floor) behind the text column (see the bell
         stage) */
      '  float dim = pow(max(dimBand(gl_FragCoord.x), u_floor), 2.2);',
      '  o = vec4((col * a + orn) * v_br * u_pre * dim, a + af * (1.0 - a));',   /* premultiplied over */
      '}'
    ].join('\n');

    /* ---- SCENE: the fringe — hundreds of hair-fine marginal strands, one
       INSTANCE each and not verlet: the strand hangs from the species'
       margin (bellShape at t = 1, evaluated right here, so the program is
       FRINGE_VS_HEAD + species GLSL + FRINGE_VS_MAIN) and bends in the
       vertex shader by the contraction wave, the lappet flutter, its own
       sway and the hero's velocity. Same expansion and coverage as the
       strands, same fragment stage. ---- */
    var FRINGE_VS_HEAD = GLSL_HEAD + [
      'in vec2 a_ks;',       /* k along (0..N-1), side -1/+1 */
      'in vec4 a_inst;',     /* phi, length jitter, phase, brightness */
      'uniform mat4 u_vp;',
      'uniform vec3 u_pos;',
      'uniform mat3 u_m;',
      'uniform float u_wave;',
      'uniform float u_time;',
      'uniform float u_waveRest;',/* the breath phase at which the species' margin is widest (at rest) */
      'uniform float u_contract;',/* species.motion.contraction: the margin's full radial contraction */
      'uniform vec3 u_vel;',      /* hero velocity, world units/s */
      'uniform float u_len;',     /* strand length, bell-local */
      'uniform float u_scale;',   /* heroScale */
      'uniform vec2 u_w;',        /* root / tip full width, bell-local */
      'uniform float u_nodes;',
      'uniform vec2 u_vpSize;',
      'uniform float u_pxPerUnit;',
      'uniform vec3 u_cam;',
      'uniform float u_br;',
      'out vec2 v_su;',
      'out float v_cov;',
      'out float v_halfPx;',
      'out float v_br;',
      'out float v_op;',
      'out float v_seed;',
      'out vec3 v_n;',
      'out vec3 v_v;',
      EXPAND_GLSL,
      SPECIES_HEAD_VS
    ].join('\n') + '\n';
    var FRINGE_VS_MAIN = [
      /* the strand in world space at fraction u of its length: hangs from
         the rim splaying slightly outward like a skirt, with its own small
         lateral lean so the hairs are not a comb; flares outward on the
         power stroke, flutters with the lappets, sways on its own phase, and
         its tip lags the bell's motion */
      'vec3 strand(float u, vec3 root, vec2 outw, float ctr, float len, float lean) {',
      '  float uu = u * u;',
      '  vec3 p = root + vec3(0.0, -u * len, 0.0);',
      '  p.xz += outw * ((u * 0.10 + uu * 0.22 * ctr) * len);',
      '  float sw = sin(u_time * ' + SWAY_RATE.toFixed(7) + ' + a_inst.z) * 0.10',
      '           + sin(24.0 * a_inst.x + u_time * FLUT_RATE) * 0.05;',
      '  p.xz += vec2(-outw.y, outw.x) * ((uu * sw + u * lean) * len);',
      '  return u_pos + u_m * p - u_vel * (uu * 0.12);',
      '}',
      'void main() {',
      '  float k = a_ks.x;',
      '  float side = a_ks.y;',
      '  float du = 1.0 / (u_nodes - 1.0);',
      '  float u = k * du;',
      /* the margin contraction, derived from the SPECIES' own rim: how far
         its margin radius now sits below its rest radius, as a fraction of
         its declared full contraction — so a species that retunes its wave
         can never desync the fringe from its own bell (Task 5 carry-over) */
      '  vec3 root = bellShape(1.0, a_inst.x, u_wave, u_time);',
      '  vec3 rest = bellShape(1.0, a_inst.x, u_waveRest, u_time);',
      '  float ctr = clamp((1.0 - length(root.xz) / max(length(rest.xz), 1e-4)) / max(u_contract, 1e-3), 0.0, 1.0);',
      '  vec2 outw = normalize(root.xz + vec2(1e-5, 0.0));',
      '  float len = u_len * a_inst.y;',
      '  float lean = (fract(a_inst.z * 0.7639) - 0.5) * 0.16;',
      '  vec3 p = strand(u, root, outw, ctr, len, lean);',
      '  vec3 tn = strand(min(u + du, 1.0), root, outw, ctr, len, lean) - strand(max(u - du, 0.0), root, outw, ctr, len, lean);',
      '  float w = mix(u_w.x, u_w.y, u) * u_scale;',
      '  float cov, hp;',
      '  gl_Position = expand(p, tn, side, w, cov, hp);',
      '  v_su = vec2(side, u);',
      '  v_cov = cov;',
      '  v_halfPx = hp;',
      /* per-strand brightness, and an opacity that follows it and softens
         toward the tip: a halo of fine translucent hairs, not a picket */
      '  v_br = u_br * a_inst.w;',
      '  v_op = mix(0.35, 1.0, a_inst.w) * (1.0 - 0.85 * smoothstep(0.30, 1.0, u));',
      '  v_seed = a_inst.z;',
      '  v_n = vec3(0.0, 0.0, 1.0);',
      '  v_v = normalize(p - u_cam);',
      '}'
    ].join('\n');

    /* ---- SCENE: the water blit (bilinear upsample; alpha 0 rides along) ---- */
    var BLIT_FS = GLSL_HEAD + [
      'in vec2 v_uv;',
      'out vec4 o;',
      'uniform sampler2D u_src;',
      'void main() { o = texture(u_src, v_uv); }'
    ].join('\n');

    /* ---- GOD RAYS (quarter res): radial march toward the surface light.
       Only what is brighter than the luma floor scatters, and the creature's
       coverage (scene alpha) occludes, so the bell casts light-shadows. ---- */
    var RAYS_FS = GLSL_HEAD + [
      'in vec2 v_uv; out vec4 o;',
      'uniform sampler2D u_scene;',      /* sceneTex */
      'uniform vec2 u_light;',           /* surface-light position in uv (above the hero, y ~ 1.05) */
      'uniform int u_samples;',          /* 64 / 40 / 24 by tier */
      'uniform float u_decay, u_density, u_weight;',
      /* the floor is judged in TRUE linear units (the LDR scene holds 0.25x),
         but the colour that accumulates stays in the target's pre-exposed
         units: the exposure is applied once, in the composite, so an 8-bit
         rayTex keeps the pre-exposure's headroom instead of clipping every
         ray past 1.0. In HDR u_exposure is 1 and nothing changes. */
      'uniform float u_exposure;',
      'void main() {',
      '  vec2 d = (v_uv - u_light) * (u_density / float(u_samples));',
      '  vec2 uv = v_uv; float illum = 1.0; vec3 acc = vec3(0.0);',
      '  for (int i = 0; i < 64; i++) {',
      '    if (i >= u_samples) break;',
      '    uv -= d;',
      '    vec4 s = texture(u_scene, uv);',
      '    float lum = max(dot(s.rgb, vec3(0.2126, 0.7152, 0.0722)) * u_exposure - 0.6, 0.0);',  /* light only */
      '    acc += s.rgb * lum * (1.0 - s.a) * illum * u_weight;',                    /* the bell occludes */
      '    illum *= u_decay;',
      '  }',
      '  o = vec4(acc, 1.0);',
      '}'
    ].join('\n');

    /* ---- BLOOM: dual-filter Kawase. Down = 4 diagonal taps + centre; the
       first level thresholds with a soft knee. Up = 9-tap tent, added into
       the previous level with additive blending. ---- */
    var BLOOM_DOWN_FS = GLSL_HEAD + [
      'in vec2 v_uv; out vec4 o;',
      'uniform sampler2D u_src; uniform vec2 u_texel; uniform float u_first;',
      /* the knee is judged in true linear units; the level itself stays
         pre-exposed (the composite applies the exposure once, see RAYS_FS) */
      'uniform float u_exposure;',
      'void main() {',
      '  vec3 c = texture(u_src, v_uv).rgb * 4.0;',
      '  c += texture(u_src, v_uv + vec2(-1.0, -1.0) * u_texel).rgb;',
      '  c += texture(u_src, v_uv + vec2( 1.0, -1.0) * u_texel).rgb;',
      '  c += texture(u_src, v_uv + vec2(-1.0,  1.0) * u_texel).rgb;',
      '  c += texture(u_src, v_uv + vec2( 1.0,  1.0) * u_texel).rgb;',
      '  c /= 8.0;',
      '  if (u_first > 0.5) { float l = max(c.r, max(c.g, c.b)) * u_exposure; c *= smoothstep(0.8, 1.6, l); }',
      '  o = vec4(c, 1.0);',
      '}'
    ].join('\n');

    var BLOOM_UP_FS = GLSL_HEAD + [
      'in vec2 v_uv; out vec4 o;',
      'uniform sampler2D u_src; uniform vec2 u_texel;',
      'void main() {',
      '  vec3 c = texture(u_src, v_uv + vec2(-2.0, 0.0) * u_texel).rgb;',
      '  c += texture(u_src, v_uv + vec2(-1.0, 1.0) * u_texel).rgb * 2.0;',
      '  c += texture(u_src, v_uv + vec2(0.0, 2.0) * u_texel).rgb;',
      '  c += texture(u_src, v_uv + vec2(1.0, 1.0) * u_texel).rgb * 2.0;',
      '  c += texture(u_src, v_uv + vec2(2.0, 0.0) * u_texel).rgb;',
      '  c += texture(u_src, v_uv + vec2(1.0, -1.0) * u_texel).rgb * 2.0;',
      '  c += texture(u_src, v_uv + vec2(0.0, -2.0) * u_texel).rgb;',
      '  c += texture(u_src, v_uv + vec2(-1.0, -1.0) * u_texel).rgb * 2.0;',
      '  o = vec4(c / 12.0, 1.0);',
      '}'
    ].join('\n');

    /* ---- COMPOSITE to the default framebuffer: scene + bloom + rays,
       exposure, the tone curve, the dim band, vignette, sharpen, dither ---- */
    var COMP_FS = GLSL_HEAD + [
      'in vec2 v_uv; out vec4 o;',
      'uniform sampler2D u_scene, u_bloom, u_rays;',
      'uniform vec2 u_texel;',          /* 1 / drawSize */
      'uniform float u_exposure;',      /* 1.0 HDR, 4.0 in LDR mode (undo the 0.25 pre-exposure) */
      'uniform float u_bloomAmt, u_rayAmt;',
      'uniform float u_fxaa;',          /* 1 on Low */
      HASH_GLSL,
      DIM_GLSL,                         /* u_dim: dimL, dimR (device px) and feather */
      /* TONE (Task 6 retune, ruling 3). The water layers were authored
         display-referred and converted to linear on the way in (LIN_GLSL),
         so the curve's job is to hand them back unchanged and only roll the
         creature's HDR light off above a knee. The AgX sigmoid that sat here
         did more: its toe is black at 2^-12.5 linear and maps 0.07 display to
         0.036, 0.10 to 0.067 — and the whole water gradient lives under 0.21
         display, so it came out crushed into flat black runs (judge:
         max_flat_run 14-24 / distinct_levels 33-37 against the classic's
         5-14 / 47-51). Now: the plain 2.2 display encode up to the knee
         (0.72 display: the water and mid-tones come back exactly as
         authored), then an exponential shoulder that is C1 at the knee and
         approaches 0.99 asymptotically. It is applied to the MAX channel with the display-
         space RGB ratios kept, plus a gentle path-to-white only far above
         1.0 linear, so a bright gold stays gold — the apex star never clips
         to white — and no channel can reach 255 even with the dither. */
      'vec3 tone(vec3 c) {',
      '  vec3 d = pow(max(c, vec3(0.0)), vec3(0.4545454));',   /* display-encoded */
      '  float e = max(d.r, max(d.g, d.b));',
      '  float K = 0.72;',
      '  float s = e <= K ? e : K + (0.99 - K) * (1.0 - exp(-(e - K) / (0.99 - K)));',
      '  vec3 o = d * (s / max(e, 1e-5));',
      '  return mix(o, vec3(s), 0.35 * smoothstep(1.2, 4.0, e));',
      '}',
      'vec3 sceneAt(vec2 uv) { return texture(u_scene, uv).rgb; }',
      'void main() {',
      '  vec4 sc = texture(u_scene, v_uv);',
      '  vec3 c = sc.rgb;',            /* sc.a is her coverage; the god-ray pass reads it, this pass no longer needs it */
      '  if (u_fxaa > 0.5) {',          /* cheap luma-edge blend on Low (no MSAA there) */
      '    vec3 n = sceneAt(v_uv + vec2(0.0, u_texel.y)), s = sceneAt(v_uv - vec2(0.0, u_texel.y));',
      '    vec3 e = sceneAt(v_uv + vec2(u_texel.x, 0.0)), w = sceneAt(v_uv - vec2(u_texel.x, 0.0));',
      '    float edge = length(n - s) + length(e - w);',
      '    c = mix(c, (n + s + e + w + c) * 0.2, clamp(edge * 2.0, 0.0, 0.6));',
      '  }',
      /* contrast-adaptive sharpen (CAS-lite) at the spec's 0.35, CLAMPED to
         the 5-tap neighbourhood's own range, per channel. Unclamped, it ran
         on LINEAR HDR values: beside a bright silhouette the water's undershoot,
         -0.0875 x (bell - water), took red and blue below zero while green
         stayed positive, tone() clamped the negatives, and every bright edge
         (the bell's outline, the moon's fringe hairs, the arms) wore a 1-px
         near-black line of green-tinted pixels, (0, 37, 1); the overshoot
         inside tinted the edge green. Clamped to [min, max] of the taps it can
         steepen an antialiased ramp but never ring past either side of an
         edge, so nothing goes negative and no channel is skewed (the property
         that makes real CAS ringing-free). Not tuned to the bake-off judge:
         a stronger gain, or no clamp, narrows the measured rim by adding
         overshoot, which is ringing, not crispness. The grain is added after
         it, so it is never amplified. */
      '  vec3 tn = sceneAt(v_uv + vec2(0.0, u_texel.y)), ts = sceneAt(v_uv - vec2(0.0, u_texel.y));',
      '  vec3 te = sceneAt(v_uv + vec2(u_texel.x, 0.0)), tw = sceneAt(v_uv - vec2(u_texel.x, 0.0));',
      '  vec3 blur = (tn + ts + te + tw) * 0.25;',
      '  vec3 lo = min(c, min(min(tn, ts), min(te, tw)));',
      '  vec3 hi = max(c, max(max(tn, ts), max(te, tw)));',
      '  c = clamp(c + (c - blur) * 0.35, lo, hi);',
      /* the a11y dim band, the old 3D engine's rule: the bloom and the rays
         are held down behind the text column here; the creature holds its
         own light down in the bell and strand stages (max(band, floor), that
         engine's v_dim) so a translucent wall dims by its due, not by its
         coverage. Water pixels are left alone: their light layers were
         already banded in the water pass and the gradient underneath never
         dims. */
      '  float band = dimBand(v_uv.x / u_texel.x);',
      /* RULING: the band's floor (0.35) is a DISPLAY-space factor, the old
         3D engine's number; this multiplies LINEAR values before the tone
         curve, so it is converted with pow(., 2.2) to read as it did there */
      '  float bandL = pow(max(band, 0.0), 2.2);',
      /* the ONE place the exposure is applied: scene, bloom and rays all
         arrive in the targets' pre-exposed units (0.25x in LDR) */
      '  c = (c',
      '    + texture(u_bloom, v_uv).rgb * (u_bloomAmt * bandL)',
      '    + texture(u_rays, v_uv).rgb * (u_rayAmt * bandL)) * u_exposure;',
      '  vec3 col = tone(c);',
      /* the spec's gentle vignette, display space, over everything —
         CIRCULAR on screen (the long axis spans +-0.5), not the uv ellipse:
         a uv vignette darkens a portrait phone's side edges as hard as its
         top, which took the water strip at its left edge from 54 to 50
         distinct levels; on screen-round it falls on the phone's top band
         (the header) and bottom, and on a desktop's sides and corners */
      '  vec2 sz = 1.0 / u_texel;',
      '  vec2 q = (v_uv - 0.5) * sz / max(sz.x, sz.y);',
      '  col *= 1.0 - 0.40 * dot(q, q);',
      /* dither = the old 3D engine's photographic grain, exactly: +-0.005
         display (~+-1.3 codes), a STATIC per-device-pixel hash folded into a
         64 px cell. At this amplitude a static field is invisible and breaks
         the water gradient into 3-5 px runs with that engine's level count
         (Task 6, banding gate); the animated +-0.5 LSB it replaces left ~14 px runs
         where the gradient climbs one code per ~14 rows, and a moving field
         at +-1.3 codes would read as TV static. */
      '  col += (vhash(mod(gl_FragCoord.xy, 64.0)) - 0.5) * 0.010;',
      '  o = vec4(clamp(col, 0.0, 1.0), 1.0);',
      '}'
    ].join('\n');

    /* --------------------------------------------------------------- build */
    /* the silhouettes' (t, phi) grid: the surface itself lives in the VS */
    function buildSilGrid() {
      var cols = SIL_SEG + 1, rows = SIL_RINGS + 1;
      var v = new Float32Array(rows * cols * 2);
      var o = 0, r, s;
      for (r = 0; r < rows; r++) {
        for (s = 0; s < cols; s++) {
          v[o++] = r / SIL_RINGS;
          v[o++] = (s / SIL_SEG) * TWO_PI;
        }
      }
      var idx = new Uint16Array(SIL_RINGS * SIL_SEG * 6);
      o = 0;
      for (r = 0; r < SIL_RINGS; r++) {
        for (s = 0; s < SIL_SEG; s++) {
          var a = r * cols + s, b = a + 1, c = a + cols, d = c + 1;
          idx[o++] = a; idx[o++] = c; idx[o++] = b;
          idx[o++] = b; idx[o++] = c; idx[o++] = d;
        }
      }
      return { v: v, i: idx };
    }

    function staticBuf(target, data, usage) {
      var b = res.buffer();
      gl.bindBuffer(target, b);
      gl.bufferData(target, data, usage || gl.STATIC_DRAW);
      return b;
    }

    function attr(loc, size, stride, off, div) {
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, size, gl.FLOAT, false, stride, off);
      gl.vertexAttribDivisor(loc, div);
    }

    /* Compiles every program and builds the static buffers and VAOs. Called
       at mount and after a context restore; sizeTargets() follows it (the
       callers do that — this never sizes anything). A compile or link error
       throws, and mount() turns that into a fallback. */
    function buildAll() {
      progBack = res.program(TRI_VS, BACK_FS, {});
      progBlit = res.program(TRI_VS, BLIT_FS, {});
      progRays = res.program(TRI_VS, RAYS_FS, {});
      progDown = res.program(TRI_VS, BLOOM_DOWN_FS, {});
      progUp = res.program(TRI_VS, BLOOM_UP_FS, {});
      progComp = res.program(TRI_VS, COMP_FS, {});
      progSil = res.program(SIL_VS, SIL_FS, { a_tp: 0, a_i0: 2, a_i1: 3 });
      progMote = res.program(MOTE_VS, MOTE_FS, { a_q: 0, a_i0: 2, a_i1: 3, a_tag: 5 });
      progShell = res.program(SHELL_VS, SHELL_FS, { a_q: 0, a_i0: 2, a_i1: 3 });
      progGlow = res.program(GLOW_VS, GLOW_FS, { a_q: 0 });
      /* the creature: the bell and fringe programs are the species' GLSL
         between the engine's heads (a species compile error throws here and
         mount() falls back to the next renderer); strands and fringe share
         a fragment stage */
      var spGlsl = '\n' + species.bell.glsl + '\n';
      progBell = res.program(BELL_VS_HEAD + spGlsl + BELL_VS_MAIN, BELL_FS_HEAD + spGlsl + BELL_FS_MAIN, { a_tp: 0 });
      progStrand = res.program(STRAND_VS, STRAND_FS, { a_pos: 0, a_tan: 1, a_aux: 2, a_seed: 3 });
      progFringe = res.program(FRINGE_VS_HEAD + spGlsl + FRINGE_VS_MAIN, STRAND_FS, { a_ks: 0, a_inst: 2 });
      /* sampler units are fixed for the life of each program; the single-
         sampler passes use unit 0, the default */
      gl.useProgram(progBell.p);
      gl.uniform1i(progBell.u.u_water, 0);
      gl.useProgram(progComp.p);
      gl.uniform1i(progComp.u.u_scene, 0);
      gl.uniform1i(progComp.u.u_bloom, 1);
      gl.uniform1i(progComp.u.u_rays, 2);

      /* the full-screen triangle draws from gl_VertexID: an EMPTY vao, so no
         stale attribute array from another pass is ever read */
      triVao = res.vao();

      /* no vao bound while the silhouette index buffer is made (it binds to
         ELEMENT_ARRAY_BUFFER, which is vao state); it joins silVao below */
      gl.bindVertexArray(null);
      quadBuf = staticBuf(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]));
      var grid = buildSilGrid();
      silIdxCount = grid.i.length;
      silGridBuf = staticBuf(gl.ARRAY_BUFFER, grid.v);
      silIdxBuf = staticBuf(gl.ELEMENT_ARRAY_BUFFER, grid.i);
      silBuf = staticBuf(gl.ARRAY_BUFFER, silF);
      /* the cloud is static except instance 0 (her nucleus), rewritten from
         the CPU each frame — 36 bytes, one bufferSubData, no allocation */
      moteBuf = staticBuf(gl.ARRAY_BUFFER, moteF, gl.DYNAMIC_DRAW);
      shellBuf = staticBuf(gl.ARRAY_BUFFER, shellF.byteLength, gl.DYNAMIC_DRAW);

      silVao = res.vao();
      gl.bindVertexArray(silVao);
      gl.bindBuffer(gl.ARRAY_BUFFER, silGridBuf); attr(0, 2, 0, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, silBuf); attr(2, 4, 32, 0, 1); attr(3, 4, 32, 16, 1);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, silIdxBuf);   /* part of the vao's state */

      moteVao = res.vao();
      gl.bindVertexArray(moteVao);
      gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf); attr(0, 2, 0, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, moteBuf); attr(2, 4, 36, 0, 1); attr(3, 4, 36, 16, 1); attr(5, 1, 36, 32, 1);

      shellVao = res.vao();
      gl.bindVertexArray(shellVao);
      gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf); attr(0, 2, 0, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, shellBuf); attr(2, 4, 32, 0, 1); attr(3, 4, 32, 16, 1);

      glowVao = res.vao();
      gl.bindVertexArray(glowVao);
      gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf); attr(0, 2, 0, 0, 0);

      /* the creature's buffers, sized ONCE for the species' Ultra counts: a
         tier change refills a prefix with bufferSubData and never creates or
         releases a GL object (rebuildTierMeshes). The strand stream is the
         one per-frame upload besides the nucleus and the shells. The
         element binding is VAO state: unbind glowVao first, or creating the
         index buffers below would rebind ITS element buffer. Each index
         buffer then goes into its own vao. */
      gl.bindVertexArray(null);
      bellVBuf = staticBuf(gl.ARRAY_BUFFER, bellV.byteLength, gl.DYNAMIC_DRAW);
      bellIBuf = staticBuf(gl.ELEMENT_ARRAY_BUFFER, bellI.byteLength, gl.DYNAMIC_DRAW);
      strandBuf = staticBuf(gl.ARRAY_BUFFER, strandF.byteLength, gl.DYNAMIC_DRAW);
      strandIBuf = staticBuf(gl.ELEMENT_ARRAY_BUFFER, strandIdx.byteLength, gl.DYNAMIC_DRAW);
      fringeBuf = staticBuf(gl.ARRAY_BUFFER, fringeF.byteLength, gl.DYNAMIC_DRAW);
      fringeTplBuf = staticBuf(gl.ARRAY_BUFFER, fringeTpl);

      bellVao = res.vao();
      gl.bindVertexArray(bellVao);
      gl.bindBuffer(gl.ARRAY_BUFFER, bellVBuf); attr(0, 2, 0, 0, 0);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, bellIBuf);

      strandVao = res.vao();
      gl.bindVertexArray(strandVao);
      gl.bindBuffer(gl.ARRAY_BUFFER, strandBuf);
      attr(0, 3, 44, 0, 0); attr(1, 3, 44, 12, 0); attr(2, 4, 44, 24, 0); attr(3, 1, 44, 40, 0);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, strandIBuf);

      fringeVao = res.vao();
      gl.bindVertexArray(fringeVao);
      gl.bindBuffer(gl.ARRAY_BUFFER, fringeTplBuf); attr(0, 2, 0, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, fringeBuf); attr(2, 4, 16, 0, 1);

      gl.bindVertexArray(null);
      gl.disable(gl.DEPTH_TEST);
      gl.disable(gl.CULL_FACE);
      gl.disable(gl.BLEND);
      shellCount = 0;
    }

    function keep(obj) { sized.push(obj); return obj; }

    /* the driver's own ceiling for THIS format can sit below MAX_SAMPLES */
    function maxSamplesFor(fmt) {
      var s = gl.getInternalformatParameter(gl.RENDERBUFFER, fmt, gl.SAMPLES);
      return (s && s.length) ? s[0] : 0;   /* descending: [0] is the largest */
    }

    function checkFbo(name) {
      var st = gl.checkFramebufferStatus(gl.FRAMEBUFFER);
      if (st !== gl.FRAMEBUFFER_COMPLETE && !gl.isContextLost()) {
        throw new Error('framebuffer ' + name + ' incomplete: ' + st);
      }
    }

    /* Re-creates every size-dependent target from drawW/drawH and the tier's
       render scale: waterTex (1/2), the scene MSAA buffer + sceneTex (1x),
       rayTex (1/4) and the bloom chain (halving). Runs on mount, restore,
       resize and tier change, so it first releases whatever the previous call
       built. The camera projection and the dim band depend on the same
       measurements, so they are refreshed here too. */
    function sizeTargets() {
      var i, T = TIERS[tier];
      for (i = sized.length - 1; i >= 0; i--) { res.release(sized[i]); }
      sized.length = 0;

      aspect = cssW / Math.max(1, cssH);
      tanXA = tanY * aspect;
      perspective(mProj, FOVY, aspect, NEARZ, FARZ);
      measureColumn();

      preExp = caps.hdr ? 1 : 0.25;
      exposure = caps.hdr ? 1 : 4;
      /* LDR RULING: the 8-bit targets store sRGB, not linear. The pipeline
         is linear either way (the GPU decodes on sample, encodes on store and
         blends in linear); only the STORAGE is perceptual, which is where the
         water's darks live. Linear RGBA8 at the 0.25 pre-exposure left them
         0-2 codes deep and the whole frame read as speckle. */
      var fmt = caps.hdr ? gl.RGBA16F : gl.SRGB8_ALPHA8;

      rsW = Math.max(1, Math.round(drawW * T.scale));
      rsH = Math.max(1, Math.round(drawH * T.scale));
      waterW = Math.max(1, Math.round(rsW * 0.5));
      waterH = Math.max(1, Math.round(rsH * 0.5));
      waterTex = keep(res.texture(waterW, waterH, fmt));
      waterFbo = keep(res.fbo(waterTex));
      checkFbo('water');

      sceneTex = keep(res.texture(rsW, rsH, fmt));
      sceneFbo = keep(res.fbo(sceneTex));
      checkFbo('scene');
      msaaSamples = Math.min(T.msaa, caps.maxSamples, maxSamplesFor(fmt));
      sceneMs = null;
      if (msaaSamples > 1) {
        var ms = res.msaaFbo(rsW, rsH, msaaSamples, fmt);
        keep(ms.rb); keep(ms.fbo);
        checkFbo('scene msaa');
        sceneMs = ms;
      }

      rayW = Math.max(1, Math.round(rsW * 0.25));
      rayH = Math.max(1, Math.round(rsH * 0.25));
      rayTex = keep(res.texture(rayW, rayH, fmt));
      rayFbo = keep(res.fbo(rayTex));
      checkFbo('rays');
      /* fewer samples march in bigger steps: keep the per-distance decay and
         the total weight the same across tiers */
      raySamples = T.rays;
      rayDecay = Math.exp(Math.log(0.96) * 64 / T.rays);
      rayWeight = 2.0 / T.rays;

      bloomLevels = Math.min(T.bloom, MAX_BLOOM);
      var w = rsW, h = rsH;
      for (i = 0; i < bloomLevels; i++) {
        w = Math.max(1, w >> 1); h = Math.max(1, h >> 1);
        bloomW[i] = w; bloomH[i] = h;
        bloomTex[i] = keep(res.texture(w, h, fmt));
        bloomFbo[i] = keep(res.fbo(bloomTex[i]));
        checkFbo('bloom');
      }
      for (i = bloomLevels; i < MAX_BLOOM; i++) { bloomTex[i] = null; bloomFbo[i] = null; }
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    }

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

    /* --------------------------------------------------------------- tiers */
    /* ?jellytier pins a tier for the session (and is its own ceiling). Else a
       phone-sized viewport or a small machine starts at High and never steps
       above it; everything else starts at Ultra. Reads cssW, so it runs after
       computeSize(). */
    function startingTier() {
      /* own keys only: TIERS.constructor and friends are not tiers */
      pinned = !!PARAMS.tier && Object.prototype.hasOwnProperty.call(TIERS, PARAMS.tier);
      if (pinned) { ceiling = PARAMS.tier; return PARAMS.tier; }
      var weak = cssW <= 720 || (navigator.deviceMemory && navigator.deviceMemory <= 4) ||
                 (navigator.hardwareConcurrency && navigator.hardwareConcurrency <= 4);
      ceiling = weak ? 'high' : 'ultra';
      return ceiling;
    }

    /* Once per frame with the RAF-to-RAF wall time in ms (the loop clamps it
       to 250). Down when the EMA sits above 20 ms for 2 s, up when it sits
       below 11 ms for 10 s, never within 8 s of the last change, never above
       the ceiling. Those seconds are WALL seconds summed from the same
       clamped frame time, not the sim's dt: that is capped at 50 ms, which
       would turn "2 s" into "40 frames" (13 s at 3 fps) on exactly the
       machines that need the step. ?jellyslow=N substitutes N ms for every
       measurement so a test can drive the clock; window.__jellyfieldSlow
       changes N at runtime, honoured only when the page loaded with
       ?jellyslow. The first WARM_FRAMES after a change are not measured. */
    function tierTick(frameMs) {
      if (pinned) { return; }
      var dt = frameMs / 1000;
      sinceChange += dt;
      if (warm < WARM_FRAMES) { warm++; return; }
      if (PARAMS.slow !== null) {
        var o = window.__jellyfieldSlow;
        frameMs = (typeof o === 'number' && isFinite(o) && o > 0) ? o : PARAMS.slow;
      }
      ema += (frameMs - ema) * 0.08;
      if (ema > 20) { slowFor += dt; fastFor = 0; }
      else if (ema < 11) { fastFor += dt; slowFor = 0; }
      else { slowFor = 0; fastFor = 0; }
      if (sinceChange < 8) { return; }
      var i = ORDER.indexOf(tier);
      if (slowFor > 2 && i > 0) { setTier(ORDER[i - 1]); }
      else if (fastFor > 10 && i < ORDER.indexOf(ceiling)) { setTier(ORDER[i + 1]); }
    }

    /* A tier change rebuilds only the size-dependent targets and the
       tier-dependent meshes, never the context. It runs inside the frame
       loop, so a failure (an incomplete framebuffer at the new size) must end
       the engine cleanly rather than leave half-released targets under a live
       loop: fatal() stops the loop and the selector demotes to 2D. */
    function setTier(name) {
      if (name === tier) { return; }
      sinceChange = 0;
      try { applyTier(name); } catch (err) { fatal(); }
    }

    /* the shared tail of mount, restore and setTier: the tier's targets and
       meshes, and a fresh warm-up. Throws; the callers decide (mount falls
       back to the next renderer, the loop goes fatal). */
    function applyTier(name) {
      tier = name;
      warm = 0; slowFor = 0; fastFor = 0;
      sizeTargets();
      relayout();          /* the layout feeds the chain segment lengths the meshes need */
      rebuildTierMeshes();
    }

    /* The tier's meshes: the bell grid at TIERS[tier].bellSeg x bellRings
       (Uint32 indices), the chain tables (every oral arm — anatomy — and
       tentacles x chainScale — density) with their strand index ranges, and
       the fringe instances (count x fringeScale). Runs after sizeTargets()
       and relayout() at mount, after a context restore and on every tier
       change. It builds no GL object: the CPU arrays were allocated for the
       Ultra counts by initCreature() and the GPU buffers at those sizes by
       buildAll(), so this only fills prefixes and uploads them. */
    function rebuildTierMeshes() {
      var T = TIERS[tier], gi, g, i, k, j, c, o, off, vert;
      /* the bell grid: (t, phi). Index order (a, b, c) / (b, d, c) is CCW
         seen from OUTSIDE against the outward normal d/dphi x d/dt the VS
         computes, so cullFace(BACK) keeps the near wall, cullFace(FRONT)
         the far one. */
      var seg = T.bellSeg, rings = T.bellRings, cols = seg + 1;
      o = 0;
      for (i = 0; i <= rings; i++) {
        for (k = 0; k <= seg; k++) { bellV[o++] = i / rings; bellV[o++] = (k / seg) * TWO_PI; }
      }
      var vCount = o;
      o = 0;
      for (i = 0; i < rings; i++) {
        for (k = 0; k < seg; k++) {
          var a = i * cols + k, b = a + 1, cc = a + cols, d = cc + 1;
          bellI[o++] = a; bellI[o++] = b; bellI[o++] = cc;
          bellI[o++] = b; bellI[o++] = d; bellI[o++] = cc;
        }
      }
      bellIdxCount = o;
      /* the element binding is VAO state: bind the owner first */
      gl.bindVertexArray(bellVao);
      gl.bindBuffer(gl.ARRAY_BUFFER, bellVBuf);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, bellV, 0, vCount);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, bellIBuf);
      gl.bufferSubData(gl.ELEMENT_ARRAY_BUFFER, 0, bellI, 0, bellIdxCount);

      /* the chain tables, group by group, so each group is one contiguous
         index range and one draw with its own colour and frill */
      chainsActive = 0; off = 0; vert = 0; o = 0;
      for (gi = 0; gi < groups.length; gi++) {
        g = groups[gi];
        if (g.kind === 2) { continue; }
        g.active = g.kind === 0 ? Math.min(g.count, Math.max(g.count > 0 ? 1 : 0, Math.round(g.count * T.chainScale))) : g.count;
        g.first = chainsActive;
        g.vert0 = vert; g.idx0 = o;
        for (i = 0; i < g.active; i++) {
          c = chainsActive++;
          chOff[c] = off; chLen[c] = g.nodes; chKind[c] = g.kind; chGroup[c] = gi; chVert[c] = vert;
          /* the old 3D engine's spacing: even around the margin plus a small
             per-chain jitter for the tentacles; the arms sit on their
             quadrants */
          chPhi[c] = g.phase + (i / g.active) * TWO_PI + (g.kind === 0 ? 0.06 * Math.sin((g.first + i) * 7.3) : 0);
          chSeed[c] = (g.first + i) * 2.399;
          for (k = 0; k < g.nodes - 1; k++) {
            for (j = 0; j < g.across - 1; j++) {
              var v0 = vert + k * g.across + j, v1 = v0 + 1, v2 = v0 + g.across, v3 = v2 + 1;
              strandIdx[o++] = v0; strandIdx[o++] = v1; strandIdx[o++] = v2;
              strandIdx[o++] = v1; strandIdx[o++] = v3; strandIdx[o++] = v2;
            }
          }
          off += g.nodes; vert += g.nodes * g.across;
        }
        g.idxCount = o - g.idx0;
      }
      strandIdxCount = o;
      gl.bindVertexArray(strandVao);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, strandIBuf);
      if (o > 0) { gl.bufferSubData(gl.ELEMENT_ARRAY_BUFFER, 0, strandIdx, 0, o); }
      gl.bindVertexArray(null);

      /* the fringe instances: (phi, length jitter, phase, brightness),
         fringeScale of the species' count, evenly spread with a little
         jitter. Lengths run 45-100% of the species' figure and the
         brightness is skewed low (many faint hairs, a few bright ones): a
         fringe of equal, equally lit strands reads as a comb. */
      o = 0; fringeActive = 0;
      for (gi = 0; gi < groups.length; gi++) {
        g = groups[gi];
        if (g.kind !== 2) { continue; }
        g.active = Math.round(g.count * T.fringeScale);
        g.first = fringeActive;
        for (i = 0; i < g.active; i++) {
          var fb = fr01(i * 5.1 + 9.4);
          fringeF[o++] = g.phase + ((i + 0.5 * fr01(i * 12.9898 + 1.3)) / g.active) * TWO_PI;
          fringeF[o++] = 0.45 + 0.55 * fr01(i * 78.233 + 2.1);
          fringeF[o++] = fr01(i * 3.7 + 0.7) * TWO_PI;
          fringeF[o++] = 0.25 + 0.75 * fb * fb;
        }
        fringeActive += g.active;
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, fringeBuf);
      if (o > 0) { gl.bufferSubData(gl.ARRAY_BUFFER, 0, fringeF, 0, o); }

      setChainSegs();
      resetChainNodes();
    }

    /* ------------------------------------------------------ the creature */
    /* Derives the chain groups from species.chains and allocates every CPU
       array for the species' ULTRA counts, once per mount (the species is
       fixed for the life of the engine; a context restore reuses them). */
    function initCreature() {
      var i, g, sp, chains = species.chains || [];
      groups.length = 0;
      CH_MAX = 0; NODE_MAX = 0; VERT_MAX = 0; IDX_MAX = 0; FRINGE_MAX = 0;
      for (i = 0; i < chains.length; i++) {
        sp = chains[i];
        g = {
          kind: sp.kind === 'fringe' ? 2 : (sp.kind === 'oralArm' ? 1 : 0),
          count: Math.max(0, Math.round(sp.count || 0)),
          nodes: Math.max(2, Math.round(sp.nodes || 2)),
          length: sp.length > 0 ? sp.length : 1,
          disc: sp.root === 'oralDisc',
          phase: sp.phase || 0,
          w0: sp.width ? sp.width[0] : 0.02, w1: sp.width ? sp.width[1] : 0.005,
          amp: sp.frill ? sp.frill.amp : 0, freq: sp.frill ? sp.frill.freq : 0,
          across: sp.frill ? Math.max(2, Math.round(sp.frill.across)) : 2,
          color: sp.color || [0.5, 0.5, 0.8],
          ferrules: !!sp.goldFerrules,
          first: 0, active: 0, vert0: 0, idx0: 0, idxCount: 0
        };
        if (g.kind === 2) {
          g.across = 2; g.amp = 0;
          FRINGE_MAX += g.count;
        } else {
          CH_MAX += g.count;
          NODE_MAX += g.count * g.nodes;
          VERT_MAX += g.count * g.nodes * g.across;
          IDX_MAX += g.count * (g.nodes - 1) * (g.across - 1) * 6;
        }
        groups.push(g);
      }
      ndPos = new Float32Array(NODE_MAX * 3); ndPrev = new Float32Array(NODE_MAX * 3);
      chOff = new Int32Array(CH_MAX); chLen = new Int32Array(CH_MAX);
      chPhi = new Float32Array(CH_MAX); chSeg = new Float32Array(CH_MAX);
      chKind = new Uint8Array(CH_MAX); chSeed = new Float32Array(CH_MAX);
      chGroup = new Int32Array(CH_MAX); chVert = new Int32Array(CH_MAX);
      strandF = new Float32Array(VERT_MAX * STRAND_STRIDE);
      strandIdx = new Uint32Array(IDX_MAX);
      fringeF = new Float32Array(FRINGE_MAX * 4);
      var U = TIERS.ultra;
      bellV = new Float32Array((U.bellSeg + 1) * (U.bellRings + 1) * 2);
      bellI = new Uint32Array(U.bellSeg * U.bellRings * 6);
      chainsActive = 0; fringeActive = 0; strandFloats = 0; strandIdxCount = 0; bellIdxCount = 0;
      heroInited = false;
      waveRest = findRestWave();
    }

    /* The breath phase at which the species' margin is widest — its rest
       radius — found once per mount by scanning its own marginJS (the JS
       mirror of bellShape at t = 1) over the cycle. The fringe shader
       compares bellShape(1, phi, u_wave) with bellShape(1, phi, waveRest) to
       read the margin's contraction off the species' rim itself, so the
       engine never hard-codes a species' wave shaping (Task 5 carry-over). */
    function findRestWave() {
      var best = -1, bestW = 0, k, j, r;
      for (k = 0; k < 64; k++) {
        r = 0;
        for (j = 0; j < 8; j++) {
          species.marginJS((j / 8) * TWO_PI, k / 64, 0, mgOut);
          r += Math.sqrt(mgOut[0] * mgOut[0] + mgOut[2] * mgOut[2]);
        }
        if (r > best) { best = r; bestW = k / 64; }
      }
      return bestW;
    }

    /* bell model matrix: Rz(leanZ) * Rx(leanX) * scale, column-major */
    function buildM9() {
      var cx = Math.cos(leanX), sx = Math.sin(leanX);
      var cz = Math.cos(leanZ), sz = Math.sin(leanZ);
      var s = heroScale;
      m9[0] = cz * s; m9[1] = sz * s; m9[2] = 0;
      m9[3] = -sz * cx * s; m9[4] = cz * cx * s; m9[5] = sx * s;
      m9[6] = sz * sx * s; m9[7] = -cz * sx * s; m9[8] = cx * s;
      var inv = 1 / (s * s);
      m9i[0] = m9[0] * inv; m9i[1] = m9[3] * inv; m9i[2] = m9[6] * inv;
      m9i[3] = m9[1] * inv; m9i[4] = m9[4] * inv; m9i[5] = m9[7] * inv;
      m9i[6] = m9[2] * inv; m9i[7] = m9[5] * inv; m9i[8] = m9[8] * inv;
    }

    /* HERO LAYOUT, carried over: the desktop open water beside .column and
       the phone band above the first .panel, both measured layout-relative.
       Driven by the species' swept envelope (extent.halfWidth / up / down)
       in place of the old 3D engine's BELL_HW / SWEPT_UP / SWEPT_DN /
       BELL_UP, so a species with a different silhouette budgets itself. */
    function setupHero() {
      var W = cssW, H = cssH;
      var mobile = W <= 720;
      var pxPerWorld = H / (2 * tanY * (-HERO_Z));
      var HW = species.extent.halfWidth, UP = species.extent.up, DN = species.extent.down;
      var right = 0, avail = 0, hw = 0;
      if (mobile) {
        /* The console card spans the full width here and is opaque — anchor
           the bell in the measured open-water band ABOVE it (same idea as the
           desktop .column probe) so the hero is never buried behind the card. */
        /* LAYOUT-relative, not scroll-relative: the canvas is fixed, so she is
           sized against the card's resting place. getBoundingClientRect() alone
           moves with the scroll, and a resize fired mid-scroll (the mobile URL
           bar collapsing does exactly that) read a card top far above the fold
           and shrank her to the band-floor size until the next resize. */
        var panel = document.querySelector('.panel');
        var pTop = panel
          ? panel.getBoundingClientRect().top + (window.pageYOffset || document.documentElement.scrollTop || 0)
          : H * 0.34;
        var band = Math.max(H * 0.14, Math.min(H * 0.55, pTop));
        /* SWEPT extents: the species' figures include the crown, the margin
           lobes, the flutter and the power-stroke surge, so the whole
           envelope she sweeps fits the band and the apex never clips. TUCK is
           the old 3D engine's hem allowance — the console card is
           opaque and the drape passes behind it regardless, so the rim may
           ride that far under the card's edge. */
        var TUCK = 18;
        var vBand = band + TUCK;
        heroScale = Math.max(0.55, Math.min(2.05, Math.min(
          (W * 0.88) / (2 * pxPerWorld * HW),
          (vBand - 12) / (pxPerWorld * (UP + DN)))));
        anchorPxX = W * 0.5;    /* centred: the bell is symmetric, so she is too */
        anchorPxY = Math.max(pxPerWorld * heroScale * UP + 8,
                             vBand - pxPerWorld * heroScale * DN - 4);
        heroFloor = 0.55;
      } else {
        var col = document.querySelector('.column');
        right = col ? col.getBoundingClientRect().right : W * 0.3;
        avail = Math.max(240, W - right);
        /* MONUMENTAL: she claims ~78% of the open water beside the console.
           Floor 1.15 so a cramped frame still reads; ceiling 2.45 so a very
           wide monitor does not turn her into wallpaper. */
        heroScale = Math.max(1.15, Math.min(2.45,
          (avail * 0.76) / (2 * pxPerWorld * HW)));
        hw = HW * heroScale * pxPerWorld;
        anchorPxX = right + avail * 0.5;
        /* two bounds, applied cheapest-currency-last: keep the outboard margin
           on frame, and keep at most ~10% of the bell behind the console glass.
           If they conflict (a frame with almost no open water) the FRAME EDGE
           wins, exactly as before. */
        var loX = right + 0.90 * hw;
        var hiX = W - 10 - hw;
        if (anchorPxX < loX) { anchorPxX = loX; }
        if (anchorPxX > hiX) { anchorPxX = hiX; }
        /* vertical: apex clear of the top, the drape free to fall out of frame
           the way a real medusa's tentacles do */
        anchorPxY = Math.max(pxPerWorld * heroScale * UP + 14, H * 0.44);
        /* On a ~800px frame the 600px column leaves no water at all: keeping
           her whole means sitting behind the glass. Lift the dim floor the way
           the phone layout does when that happens, or she fades to a stain. */
        heroFloor = anchorPxX < right ? 0.5 : 0;
      }
      heroMobile = mobile;
      /* +1: instance 0 is her nucleus and is never culled */
      moteCount = (mobile ? 40 : N_MOTE) + 1;
      setChainSegs();
    }

    /* segment length per active chain from the species' length and the
       layout's scale; the tentacles vary +-16% so the fringe is not a comb */
    function setChainSegs() {
      var c;
      for (c = 0; c < chainsActive; c++) {
        var g = groups[chGroup[c]];
        var vary = g.kind === 0 ? 0.84 + 0.32 * (0.5 + 0.5 * Math.sin(c * 12.9898)) : 1;
        chSeg[c] = g.length * vary * heroScale / (chLen[c] - 1);
      }
    }

    /* layout after any size change: setupHero() re-anchors and re-scales
       her; the spring and the chains are reset only when she has never been
       placed or the phone/desktop breakpoint flipped (the old 3D engine's
       rule), so a plain resize never snaps her. The box is refreshed from
       the camera at rest so info() is right before the first frame. */
    function relayout() {
      var wasMobile = heroMobile;
      setupHero();
      if (!heroInited || wasMobile !== heroMobile) {
        heroInited = true;
        resetHeroAnchor();
        resetChainNodes();
      }
      updateCamera(0);
      buildM9();
      updateHeroBox();
    }

    function resetHeroAnchor() {
      var nx = (anchorPxX / Math.max(1, cssW)) * 2 - 1;
      var ny = 1 - (anchorPxY / Math.max(1, cssH)) * 2;
      heroX = nx * tanXA * (-HERO_Z);
      heroY = ny * tanY * (-HERO_Z);
      heroVX = 0; heroVY = 0;
      heroPX = heroX; heroPY = heroY;
      leanX = 0.30; leanZ = 0;
      buildM9();
    }

    /* the chain's root in bell-local units, into mgOut: the species' margin
       mirror (the old 3D engine's bellMarginLocal), or the oral disc — that
       engine's ring of radius 0.26 just under the subumbrella */
    function chainRoot(c) {
      if (groups[chGroup[c]].disc) {
        mgOut[0] = Math.cos(chPhi[c]) * 0.26; mgOut[1] = -0.04; mgOut[2] = Math.sin(chPhi[c]) * 0.26;
      } else {
        species.marginJS(chPhi[c], pulsePhase / TWO_PI, flutPh, mgOut);
      }
    }

    /* hang every active chain straight down from its root: the first frame
       and any tier change (new chains) start from rest, not from garbage */
    function resetChainNodes() {
      var c, k;
      for (c = 0; c < chainsActive; c++) {
        var off = chOff[c], len = chLen[c];
        chainRoot(c);
        var rx = heroPX + m9[0] * mgOut[0] + m9[3] * mgOut[1] + m9[6] * mgOut[2];
        var ry = heroPY + m9[1] * mgOut[0] + m9[4] * mgOut[1] + m9[7] * mgOut[2];
        var rz = HERO_Z + m9[2] * mgOut[0] + m9[5] * mgOut[1] + m9[8] * mgOut[2];
        for (k = 0; k < len; k++) {
          var i3 = (off + k) * 3;
          ndPos[i3] = rx; ndPos[i3 + 1] = ry - chSeg[c] * k; ndPos[i3 + 2] = rz;
          ndPrev[i3] = ndPos[i3]; ndPrev[i3 + 1] = ndPos[i3 + 1]; ndPrev[i3 + 2] = ndPos[i3 + 2];
        }
      }
    }

    /* info().heroBox: the bell's TRUE OUTER SILHOUETTE in CSS px, through
       her live pose and the view-projection. The bake-off judge centres its
       rim-finding ellipse on this box and frames its crops from it, and the
       layout tests read it, so it must be the bell's real outline:
       - x: a ring of radius extent.halfWidth at the bell's origin height
         (y = 0), where both species' bells are widest: their equators (the
         moon's r 1.0 at t 0.77; the striped dome's r 1.0 at t ~0.90, where
         its th = 1.75 t crosses pi/2). halfWidth is measured as the radius
         whose projected ring covers the dome's flanks (1.0 plus the near
         flank's perspective); projected through the live pose, the ring
         also carries the perspective bulge of the flank farther from the
         screen centre. Only its x counts: its near and far points lie
         inside the silhouette, not on it.
       - top: the apex at extent.up through the live pose (the engine has no
         JS mirror of bellShape at t = 0). extent.up is the swept figure the
         layout budgets — the max over the layouts, with the crown lift, the
         surge and the perspective — so the box top BOUNDS the leaned dome's
         top rather than touching it: it sits above it by the slack between
         this layout's own envelope and that max (striped at 1440x900@1:
         ~36 px at the tightest pose, because the phone's perspective sets
         its up).
       - bottom: the margin at wave 0 (no contraction, no flutter), from
         marginJS: its near lobes are the lowest part of the bell at rest
         (the power stroke drops the lappets a little below it).
       WHY not the margin ring alone, as before: marginJS is the ROOT ring,
       tucked under the rim (r ~0.83 for the moon), so its x-span sat ~17%
       inside the equator; the judge's ellipse then put the true flank
       silhouette at its search limit and locked onto the gold meridians
       inside it, which read as a wide, soft "rim". */
    function updateHeroBox() {
      var minX = 1e9, minY = 1e9, maxX = -1e9, maxY = -1e9, i;
      var R = species.extent.halfWidth;
      for (i = 0; i <= 32; i++) {
        /* 0-15 the margin (x and y), 16-31 the widest ring (x only), 32 the apex */
        if (i < 16) { species.marginJS((i / 16) * TWO_PI, 0, 0, mgOut); }
        else if (i < 32) {
          mgOut[0] = Math.cos(((i - 16) / 16) * TWO_PI) * R; mgOut[1] = 0;
          mgOut[2] = Math.sin(((i - 16) / 16) * TWO_PI) * R;
        } else { mgOut[0] = 0; mgOut[1] = species.extent.up; mgOut[2] = 0; }
        var wx = heroPX + m9[0] * mgOut[0] + m9[3] * mgOut[1] + m9[6] * mgOut[2];
        var wy = heroPY + m9[1] * mgOut[0] + m9[4] * mgOut[1] + m9[7] * mgOut[2];
        var wz = HERO_Z + m9[2] * mgOut[0] + m9[5] * mgOut[1] + m9[8] * mgOut[2];
        var cw = mVP[3] * wx + mVP[7] * wy + mVP[11] * wz + mVP[15];
        if (cw < 0.001) { continue; }
        /* CSS px, not device px: (ndc * 0.5 + 0.5) x cssW / cssH */
        var sx = ((mVP[0] * wx + mVP[4] * wy + mVP[8] * wz + mVP[12]) / cw * 0.5 + 0.5) * cssW;
        if (sx < minX) { minX = sx; }
        if (sx > maxX) { maxX = sx; }
        if (i >= 16 && i < 32) { continue; }
        var sy = (0.5 - (mVP[1] * wx + mVP[5] * wy + mVP[9] * wz + mVP[13]) / cw * 0.5) * cssH;
        if (sy < minY) { minY = sy; }
        if (sy > maxY) { maxY = sy; }
      }
      if (maxX >= minX && maxY >= minY) { hbX = minX; hbY = minY; hbW = maxX - minX; hbH = maxY - minY; }
    }

    /* ------------------------------------------------------------ hero sim */
    function heroVerlet(dt) {
      var damp = Math.exp(-dt * 2.2);
      var dt2 = dt * dt;
      var wantStir = mouseIn && cursorA > 0.05;
      var c, k, i3, j3;
      for (c = 0; c < chainsActive; c++) {
        var off = chOff[c], len = chLen[c];
        var kind = chKind[c];
        chainRoot(c);
        i3 = off * 3;
        ndPos[i3] = heroPX + m9[0] * mgOut[0] + m9[3] * mgOut[1] + m9[6] * mgOut[2];
        ndPos[i3 + 1] = heroPY + m9[1] * mgOut[0] + m9[4] * mgOut[1] + m9[7] * mgOut[2];
        ndPos[i3 + 2] = HERO_Z + m9[2] * mgOut[0] + m9[5] * mgOut[1] + m9[8] * mgOut[2];
        ndPrev[i3] = ndPos[i3]; ndPrev[i3 + 1] = ndPos[i3 + 1]; ndPrev[i3 + 2] = ndPos[i3 + 2];
        var seed = chSeed[c];
        for (k = 1; k < len; k++) {
          i3 = (off + k) * 3;
          var x = ndPos[i3], y = ndPos[i3 + 1], z = ndPos[i3 + 2];
          var vx = (x - ndPrev[i3]) * damp;
          var vy = (y - ndPrev[i3 + 1]) * damp;
          var vz = (z - ndPrev[i3 + 2]) * damp;
          var u = k / (len - 1);
          /* gentle abyssal current + per-chain sway */
          var ax = Math.sin(timeS * 0.55 + u * 4.3 + seed) * 0.34 + Math.sin(timeS * 0.21 + seed * 2.0) * 0.15;
          var az = Math.cos(timeS * 0.47 + u * 3.6 + seed * 1.3) * 0.26;
          var ay = -1.35 * (kind === 0 ? 1.0 : 0.75);   /* slow, water-buoyant sink */
          if (kind === 1) { ax *= 1.8; az *= 1.8; }
          /* idle S-curves: a lazy sinusoid travels root->tip, phase offset
             per chain, so the tentacles wave even in a still frame */
          var sAmp = u * (kind === 0 ? 1.05 : 0.5) * (0.7 + 0.3 * Math.sin(seed * 3.7));
          ax += Math.sin(timeS * 0.85 - u * 5.6 + seed) * sAmp;
          az += Math.cos(timeS * 0.74 - u * 4.4 + seed * 1.7 + 1.3) * sAmp * 0.8;
          /* short-lived post-poke kink: high-frequency zigzag down the chain */
          if (kink > 0.01) {
            var kf = kink * 6.0 * u;
            ax += Math.sin(u * 14.0 - timeS * 21.0 + seed) * kf;
            az += Math.cos(u * 12.0 - timeS * 18.0 + seed * 1.7) * kf * 0.7;
          }
          if (wantStir) {
            /* nearby nodes stir/curl toward the pointer ray */
            var rx2 = x - eyeX, ry2 = y - eyeY, rz2 = z - eyeZ;
            var along = rx2 * rayDX + ry2 * rayDY + rz2 * rayDZ;
            if (along > 1) {
              var qx = rx2 - rayDX * along, qy = ry2 - rayDY * along, qz = rz2 - rayDZ * along;
              var q2 = qx * qx + qy * qy + qz * qz;
              var R = 1.7 + along * 0.10;
              if (q2 < R * R && q2 > 1e-4) {
                var qd = Math.sqrt(q2);
                var tt2 = 1 - qd / R;
                tt2 = tt2 * tt2 * (3 - 2 * tt2);
                var pull = 6.5 * tt2 * cursorA * u;
                ax -= qx / qd * pull; ay -= qy / qd * pull; az -= qz / qd * pull;
                var cxr = rayDY * qz - rayDZ * qy;
                var cyr = rayDZ * qx - rayDX * qz;
                var czr = rayDX * qy - rayDY * qx;
                var curl = 2.4 * tt2 * cursorA;
                ax += cxr / qd * curl; ay += cyr / qd * curl; az += czr / qd * curl;
              }
            }
          }
          ndPrev[i3] = x; ndPrev[i3 + 1] = y; ndPrev[i3 + 2] = z;
          ndPos[i3] = x + vx + ax * dt2;
          ndPos[i3 + 1] = y + vy + ay * dt2;
          ndPos[i3 + 2] = z + vz + az * dt2;
        }
        /* distance constraints: root is pinned, whip travels down the chain */
        var seg = chSeg[c];
        var it;
        for (it = 0; it < 3; it++) {
          for (k = 1; k < len; k++) {
            i3 = (off + k) * 3; j3 = (off + k - 1) * 3;
            var dx = ndPos[i3] - ndPos[j3];
            var dy = ndPos[i3 + 1] - ndPos[j3 + 1];
            var dz = ndPos[i3 + 2] - ndPos[j3 + 2];
            var d = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1e-6;
            var diff = (d - seg) / d;
            if (k === 1) {
              ndPos[i3] -= dx * diff; ndPos[i3 + 1] -= dy * diff; ndPos[i3 + 2] -= dz * diff;
            } else {
              var hx = dx * diff * 0.5, hy = dy * diff * 0.5, hz = dz * diff * 0.5;
              ndPos[j3] += hx; ndPos[j3 + 1] += hy; ndPos[j3 + 2] += hz;
              ndPos[i3] -= hx; ndPos[i3 + 1] -= hy; ndPos[i3 + 2] -= hz;
            }
          }
        }
        /* INEXTENSIBLE (E-S). Three Gauss-Seidel passes cannot hold a long
           chain taut under gravity: the stretch they leave grows with the
           node count and with dt. Measured on this engine (B's 192-node
           arms, 6 s of sim), the drawn arm was +21% long at 16 ms frames,
           +42% at 33 ms and +49% at the 50 ms cap, and still growing. The
           drape therefore depended on the frame rate and on how finely a
           species sampled its strands. One follow-the-leader pass from the
           pinned root caps every link at seg, so a chain is never longer
           than its own length, whatever the frame rate or node count.
           It only shortens: a slack link keeps its sag, and the passes
           above still do the shaping.
           THE VELOCITY TERM is load-bearing. Follow-the-leader only ever
           pulls a node toward the root, so a correction left in the verlet
           velocity (ndPos moved, ndPrev not) is a one-way push: every rise
           of the root yanks the whole chain up, and nothing pushes it back
           when the root falls. Measured: the arms climbed and coiled over
           the bell within 2-4 s at every dt. Dynamic follow-the-leader
           (Mueller, Kim and Chentanez 2012) cancels it: a node's correction
           d_k is taken back out of its PARENT's velocity, ndPrev[k-1] +=
           s d_k, with the method's damping factor s in [0, 1]. A yank then
           moves the chain without launching it, while gravity and the
           current keep their effect. s = 0.9, not 1: just under 1 leaves a
           tenth of each correction in the motion as damping, the cautious
           end; measured, 1.0 and 0.9 hang within 2% of each other and both
           are frame-rate independent, so it is not a tuned look. O(N), no
           allocation. */
        var seg2 = seg * seg;
        for (k = 1; k < len; k++) {
          i3 = (off + k) * 3; j3 = i3 - 3;
          var lx = ndPos[i3] - ndPos[j3];
          var ly = ndPos[i3 + 1] - ndPos[j3 + 1];
          var lz = ndPos[i3 + 2] - ndPos[j3 + 2];
          var l2 = lx * lx + ly * ly + lz * lz;
          if (l2 > seg2) {
            var ls = seg / Math.sqrt(l2) - 1;
            var cx = lx * ls, cy = ly * ls, cz = lz * ls;   /* d_k: pulls k back to seg */
            ndPos[i3] += cx; ndPos[i3 + 1] += cy; ndPos[i3 + 2] += cz;
            if (k > 1) {   /* the root is pinned: its velocity is not the sim's */
              ndPrev[j3] += 0.9 * cx; ndPrev[j3 + 1] += 0.9 * cy; ndPrev[j3 + 2] += 0.9 * cz;
            }
          }
        }
      }
    }

    function smooth01(x) {
      if (x <= 0) { return 0; }
      if (x >= 1) { return 1; }
      return x * x * (3 - 2 * x);
    }

    /* the strand vertex stream (preallocated): one record per (node, column)
       — position, the node's tangent (next - prev), the column's side, u,
       the full width in world units, the brightness and the chain seed. The
       vertex shader does the camera-facing expansion in device pixels. */
    function buildStrands() {
      var o = 0, c, k, j;
      for (c = 0; c < chainsActive; c++) {
        var off = chOff[c], len = chLen[c], g = groups[chGroup[c]], across = g.across;
        /* strands rooted on the FAR side of the margin are seen through the
           whole thickness of the jelly: dim them so the near fringe reads in
           front of the body instead of a flat picket across the bell */
        var farD = g.kind === 0
          ? 1 - 0.66 * smooth01((HERO_Z - ndPos[off * 3 + 2]) / (0.85 * heroScale))
          : 1;
        var br = heroBright * farD * (g.kind === 0
          ? 0.80 + 0.36 * (0.5 + 0.5 * Math.sin(c * 5.7 + 1.3))
          : 1.30);
        var seed = chSeed[c];
        for (k = 0; k < len; k++) {
          var i3 = (off + k) * 3;
          var k0 = k > 0 ? k - 1 : 0, k1 = k < len - 1 ? k + 1 : len - 1;
          var a3 = (off + k0) * 3, b3 = (off + k1) * 3;
          var tx = ndPos[b3] - ndPos[a3], ty = ndPos[b3 + 1] - ndPos[a3 + 1], tz = ndPos[b3 + 2] - ndPos[a3 + 2];
          var u = k / (len - 1);
          /* root width collapsing fast to the tip width: the strand GROWS
             out of the mantle and thins to a filament */
          var w = heroScale * (g.w1 + (g.w0 - g.w1) * Math.pow(1 - u, 3));
          for (j = 0; j < across; j++) {
            strandF[o++] = ndPos[i3]; strandF[o++] = ndPos[i3 + 1]; strandF[o++] = ndPos[i3 + 2];
            strandF[o++] = tx; strandF[o++] = ty; strandF[o++] = tz;
            strandF[o++] = -1 + 2 * j / (across - 1); strandF[o++] = u; strandF[o++] = w; strandF[o++] = br;
            strandF[o++] = seed;
          }
        }
      }
      strandFloats = o;
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
      rayDX = urx; rayDY = ury; rayDZ = urz;   /* the pointer ray the stir reads */
    }

    /* ----------------------------------------------------------------- sim */
    /* the water's own per-frame state; scalar math only, no allocation */
    function sim(dt, tMs) {
      activity += (activityTarget - activity) * Math.min(1, dt * 1.6);
      if (Math.abs(activityTarget - activity) < 0.002) { activity = activityTarget; }
      var wantCursor = mouseIn && (tMs - lastMouseT < 2000);
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
        /* BIRTH RAMP: the sonar emerges from her and brightens as it clears
           the bell, so shell + crown never stack to white on the pulse frame */
        var g = sh.r / sh.maxR;
        var born = g < 0.26 ? g / 0.26 : 1;
        born = born * born * (3 - 2 * born);
        sh.alpha = f * f * born * Math.min(1, sh.strength);
      }
      if (flash.active) {
        flash.t += dt;
        if (flash.t >= flash.dur) { flash.active = false; }
      }
      /* the breath: traveling contraction, activity drives the rate */
      pulsePhase += (TWO_PI / species.breath) * (0.8 + 0.7 * activity) * dt;
      if (pulsePhase > TWO_PI) { pulsePhase -= TWO_PI; }
      flare *= Math.max(0, 1 - dt * 1.8);
      kink *= Math.max(0, 1 - dt * 1.9);
      /* click ripple sweeping the star-motes: radius expands, light decays
         (both bounded — the radius freezes once the amplitude dies) */
      if (ripA > 0) {
        ripR += 14 * dt;
        ripA *= Math.exp(-dt * 1.9);
        if (ripA < 0.01) { ripA = 0; ripR = 0; }
      }
      /* the bounded creature clock and its phases */
      cTime = timeS % CREATURE_T;
      flutPh = (cTime * FLUT_RATE) % TWO_PI;
      wobPh = (cTime * WOB_RATE) % TWO_PI;

      /* soft anchor spring toward the layout anchor (recomputed vs the
         swaying camera each frame, so the Medusa breathes in place) */
      var nx = (anchorPxX / Math.max(1, cssW)) * 2 - 1;
      var ny = 1 - (anchorPxY / Math.max(1, cssH)) * 2;
      computeRay(nx, ny);
      var tA = urz < -0.001 ? (HERO_Z - eyeZ) / urz : -HERO_Z;
      var tgx = eyeX + urx * tA, tgy = eyeY + ury * tA;
      computeRay(mouseNX, mouseNY);   /* restore the cursor scratch ray */
      heroVX += ((tgx - heroX) * 3.0 - heroVX * 2.4) * dt;
      heroVY += ((tgy - heroY) * 3.0 - heroVY * 2.4) * dt;
      heroX += heroVX * dt; heroY += heroVY * dt;
      /* slight upward surge as the power stroke reaches the margin */
      heroPX = heroX;
      heroPY = heroY + 0.07 * heroScale * Math.sin(pulsePhase - 3.3);

      /* bell lean: presentation tilt (dome face to the camera) + cursor + idle roll */
      var ltx = 0.30 + (mouseIn ? -mouseNY * 0.10 : 0) + Math.sin(timeS * 0.33) * 0.05;
      var ltz = (mouseIn ? mouseNX * 0.12 : 0) + Math.sin(timeS * 0.26 + 1.7) * 0.06 - heroVX * 0.06;
      leanX += (ltx - leanX) * Math.min(1, dt * 1.8);
      leanZ += (ltz - leanZ) * Math.min(1, dt * 1.8);
      buildM9();

      /* shading drivers (bounded uniforms; never raw unbounded time in a shader) */
      shaftA = Math.sin(timeS * 0.31); shaftB = Math.sin(timeS * 0.23 + 2.1);
      shaftC = Math.sin(timeS * 0.27 + 4.2); shaftD = Math.sin(timeS * 0.19 + 1.1);
      starTw = (timeS * 2.7) % TWO_PI;
      var hdx = heroPX - eyeX, hdy = heroPY - eyeY, hdz = HERO_Z - eyeZ;
      var hDist = Math.sqrt(hdx * hdx + hdy * hdy + hdz * hdz);
      /* saturating flare drive: the surge shows, the stack never runs away */
      var flL = Math.min(flare, 1.5);
      var flS = flL / (1 + 0.55 * flL);
      heroBright = Math.exp(-hDist * FOG_D) * (0.9 + 0.25 * activity) * (1 + 0.5 * flS);
      starInt = heroBright * (0.58 + 0.10 * Math.sin(timeS * 2.1) + 0.07 * Math.sin(timeS * 3.7)) *
        (1 + 0.58 * flS);

      /* contrast staging: project the hero into backdrop uv space so the
         shafts converge on her, the god-ray light hangs above her and the
         water directly behind her runs a stop darker (scalar math only) */
      var hcw = mVP[3] * heroPX + mVP[7] * heroPY + mVP[11] * HERO_Z + mVP[15];
      if (hcw > 0.001) {
        heroU = (mVP[0] * heroPX + mVP[4] * heroPY + mVP[8] * HERO_Z + mVP[12]) / hcw * 0.5 + 0.5;
        heroV = (mVP[1] * heroPX + mVP[5] * heroPY + mVP[9] * HERO_Z + mVP[13]) / hcw * 0.5 + 0.5;
      }
      heroR = heroScale * 1.7 / (2 * tanY * hDist);
      heroDk = 0.38 * Math.min(1, heroBright + 0.3);

      heroVerlet(dt);
      buildStrands();
      if (frames % 30 === 0) { updateHeroBox(); }

      /* instance 0 = her nucleus — the apex star — hung on the bell's up
         vector at the species' apexY (contract): A's 0.66 sits just over its
         0.60 crown; a fixed 0.66 buried B's star inside its 0.90 dome */
      moteHead[0] = heroPX + m9[3] * species.apexY;
      moteHead[1] = heroPY + m9[4] * species.apexY;
      moteHead[2] = HERO_Z + m9[5] * species.apexY;
      moteHead[3] = heroScale * (0.66 + 0.16 * flL);
      moteHead[7] = starInt;
      moteHead[8] = 9;

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

    /* -------------------------------------------------------------- passes */
    function glCheck(pass) {
      if (!PARAMS.debug || gl.isContextLost()) { return; }
      var err = gl.getError();
      if (err !== gl.NO_ERROR) { console.error('GL error ' + err + ' after ' + pass); }
    }

    function drawGlowQuad(x, y, z, size, cr, cg, cb, intensity) {
      gl.useProgram(progGlow.p);
      gl.uniformMatrix4fv(progGlow.u.u_vp, false, mVP);
      gl.uniform3f(progGlow.u.u_pos, x, y, z);
      gl.uniform3f(progGlow.u.u_right, rgt[0], rgt[1], rgt[2]);
      gl.uniform3f(progGlow.u.u_up, upv[0], upv[1], upv[2]);
      gl.uniform1f(progGlow.u.u_size, size);
      gl.uniform3f(progGlow.u.u_color, cr, cg, cb);
      gl.uniform1f(progGlow.u.u_int, intensity);
      gl.uniform1f(progGlow.u.u_pre, preExp);
      gl.uniform3f(progGlow.u.u_dim, wDimL, wDimR, wDimF);
      gl.uniform1f(progGlow.u.u_floor, heroFloor);
      gl.bindVertexArray(glowVao);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }

    /* 1. WATER at half scale: backdrop, then additive light layers. Every
       fragment here writes alpha 0 — coverage belongs to the creature. */
    function drawWater() {
      /* the band is measured in CSS px; this target is drawn at half the
         device size, so scale it into the water's own fragment coordinates */
      var ws = dprEff * waterW / Math.max(1, drawW);
      wDimL = dimL * ws; wDimR = dimR * ws; wDimF = DIM_FEATHER * ws;
      gl.bindFramebuffer(gl.FRAMEBUFFER, waterFbo);
      gl.viewport(0, 0, waterW, waterH);
      gl.disable(gl.BLEND);
      gl.useProgram(progBack.p);
      gl.uniform4f(progBack.u.u_shaft, shaftA, shaftB, shaftC, shaftD);
      gl.uniform1f(progBack.u.u_activity, activity);
      gl.uniform1f(progBack.u.u_aspect, aspect);
      gl.uniform4f(progBack.u.u_hero, heroU, heroV, heroR, heroDk);
      gl.uniform1f(progBack.u.u_pre, preExp);
      gl.uniform1f(progBack.u.u_dither, caps.hdr ? 0 : 1);
      gl.bindVertexArray(triVao);
      gl.drawArrays(gl.TRIANGLES, 0, 3);

      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ONE);

      /* cursor light billboard at mid-depth along the pointer ray */
      if (cursorA > 0.01 && urz < -0.001) {
        var ct = (CURSOR_Z - eyeZ) / urz;
        drawGlowQuad(eyeX + urx * ct, eyeY + ury * ct, eyeZ + urz * ct,
          3.2 + activity * 1.0, 0.35, 0.62, 0.80, cursorA * 0.30);
      }

      /* distant silhouettes (one instanced draw, static instance buffer) */
      gl.useProgram(progSil.p);
      gl.uniformMatrix4fv(progSil.u.u_vp, false, mVP);
      gl.uniform3f(progSil.u.u_cam, eyeX, eyeY, eyeZ);
      gl.uniform1f(progSil.u.u_time, timeS % MOTE_T);
      gl.uniform1f(progSil.u.u_fogD, FOG_D);
      gl.uniform1f(progSil.u.u_pre, preExp);
      gl.uniform3f(progSil.u.u_dim, wDimL, wDimR, wDimF);
      gl.bindVertexArray(silVao);
      gl.drawElementsInstanced(gl.TRIANGLES, silIdxCount, gl.UNSIGNED_SHORT, 0, N_SIL);

      /* the rising dust + her nucleus: one instanced draw */
      gl.useProgram(progMote.p);
      gl.uniformMatrix4fv(progMote.u.u_vp, false, mVP);
      gl.uniform3f(progMote.u.u_right, rgt[0], rgt[1], rgt[2]);
      gl.uniform3f(progMote.u.u_up, upv[0], upv[1], upv[2]);
      gl.uniform3f(progMote.u.u_hpos, heroPX, heroPY, HERO_Z);
      gl.uniform1f(progMote.u.u_time, timeS % MOTE_T);
      gl.uniform1f(progMote.u.u_hs, heroScale);
      gl.uniform1f(progMote.u.u_act, activity);
      gl.uniform4f(progMote.u.u_rip, ripX, ripY, ripZ, ripR);
      gl.uniform1f(progMote.u.u_ripA, ripA);
      gl.uniform1f(progMote.u.u_tw, starTw);
      gl.uniform1f(progMote.u.u_pre, preExp);
      gl.uniform3f(progMote.u.u_dim, wDimL, wDimR, wDimF);
      gl.uniform1f(progMote.u.u_floor, heroFloor);
      gl.bindBuffer(gl.ARRAY_BUFFER, moteBuf);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, moteHead);   /* instance 0, rewritten in place */
      gl.bindVertexArray(moteVao);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, moteCount);

      /* shockwave shells (one instanced draw, <= 6 quads) */
      if (shellCount > 0) {
        gl.useProgram(progShell.p);
        gl.uniformMatrix4fv(progShell.u.u_vp, false, mVP);
        gl.uniform3f(progShell.u.u_right, rgt[0], rgt[1], rgt[2]);
        gl.uniform3f(progShell.u.u_up, upv[0], upv[1], upv[2]);
        gl.uniform1f(progShell.u.u_pre, preExp);
        gl.uniform3f(progShell.u.u_dim, wDimL, wDimR, wDimF);
        gl.bindBuffer(gl.ARRAY_BUFFER, shellBuf);
        gl.bufferSubData(gl.ARRAY_BUFFER, 0, shellF);
        gl.bindVertexArray(shellVao);
        gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, shellCount);
      }

      /* sonar flash at the pulse origin */
      if (flash.active) {
        var ft = 1 - flash.t / flash.dur;
        drawGlowQuad(flash.x, flash.y, flash.z,
          3.0 + (1 - ft) * 4.5, 0.55, 0.83, 0.95, ft * ft * 0.85);
      }
      gl.disable(gl.BLEND);
      gl.bindVertexArray(null);
    }

    /* THE CREATURE, into the scene target over the water blit: the bell's
       far wall, its near wall, then the strands (tentacles, frilled arms)
       and the fringe — the brief's order. Everything blends premultiplied-
       over, so the scene alpha accumulates the union of the coverages: that
       is what the god-ray pass occludes with. The bell refracts waterTex
       (already on unit 0 from the blit; bound again to be explicit), never
       the scene target it is drawing into. The a11y band IS applied here:
       the bell, strand and fringe stages each hold their own light down to
       max(band, floor) (u_dim / u_floor below); the composite then dims only
       the bloom and the rays. */
    function drawCreature() {
      var gi, g, pal = species.palette, gold = pal.gold;
      var rim = pal.rim || [0.8, 0.85, 1.0], body = pal.body || [0.5, 0.45, 0.8];
      var pxPerUnit = rsH / (2 * tanY);   /* device px per world unit at clip w = 1 */
      /* the a11y band in the scene target's pixels (CSS px x the true
         css->buffer ratio x the tier's render scale) */
      var ss = dprEff * rsW / Math.max(1, drawW);
      var sDimL = dimL * ss, sDimR = dimR * ss, sDimF = DIM_FEATHER * ss;
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);

      gl.useProgram(progBell.p);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, waterTex);
      gl.uniformMatrix4fv(progBell.u.u_vp, false, mVP);
      gl.uniform3f(progBell.u.u_pos, heroPX, heroPY, HERO_Z);
      gl.uniformMatrix3fv(progBell.u.u_m, false, m9);
      gl.uniformMatrix3fv(progBell.u.u_mi, false, m9i);
      gl.uniform1f(progBell.u.u_wave, pulsePhase / TWO_PI);
      gl.uniform1f(progBell.u.u_time, cTime);
      gl.uniform2f(progBell.u.u_vpSize, rsW, rsH);
      gl.uniform3f(progBell.u.u_cam, eyeX, eyeY, eyeZ);
      gl.uniform3f(progBell.u.u_viewR, rgt[0], rgt[1], rgt[2]);
      gl.uniform3f(progBell.u.u_viewU, upv[0], upv[1], upv[2]);
      gl.uniform1i(progBell.u.u_organSteps, TIERS[tier].organ);
      gl.uniform1f(progBell.u.u_glow, heroBright);
      gl.uniform1f(progBell.u.u_pre, preExp);
      gl.uniform3f(progBell.u.u_gold, gold[0], gold[1], gold[2]);
      gl.uniform3f(progBell.u.u_rim, rim[0], rim[1], rim[2]);
      gl.uniform3f(progBell.u.u_body, body[0], body[1], body[2]);
      gl.uniform3f(progBell.u.u_dim, sDimL, sDimR, sDimF);
      gl.uniform1f(progBell.u.u_floor, heroFloor);
      gl.bindVertexArray(bellVao);
      /* two walls: the far one first (cull the near faces), then the glassy
         near one over it — the eye sees the inside of the back of the bell
         through the front, which is what makes it read as a volume. The
         base coverages (0.45 far, 0.62 near; Fresnel and gold raise them in
         the shader) leave the far wall's gold and rim showing through the
         near glass: at the old 0.85 the near wall hid 93% of it and the bell
         read as an opaque dome. Only the near wall marches the organs. */
      gl.enable(gl.CULL_FACE);
      gl.cullFace(gl.FRONT);
      gl.uniform1f(progBell.u.u_cov, 0.45);
      gl.uniform1f(progBell.u.u_wall, 0);
      gl.drawElements(gl.TRIANGLES, bellIdxCount, gl.UNSIGNED_INT, 0);
      gl.cullFace(gl.BACK);
      gl.uniform1f(progBell.u.u_cov, 0.62);
      gl.uniform1f(progBell.u.u_wall, 1);
      gl.drawElements(gl.TRIANGLES, bellIdxCount, gl.UNSIGNED_INT, 0);
      gl.disable(gl.CULL_FACE);

      /* strands: ONE upload, one draw per chain group (its colour, frill) */
      if (strandIdxCount > 0) {
        gl.useProgram(progStrand.p);
        gl.uniformMatrix4fv(progStrand.u.u_vp, false, mVP);
        gl.uniform2f(progStrand.u.u_vpSize, rsW, rsH);
        gl.uniform1f(progStrand.u.u_pxPerUnit, pxPerUnit);
        gl.uniform3f(progStrand.u.u_cam, eyeX, eyeY, eyeZ);
        gl.uniform1f(progStrand.u.u_time, cTime);
        gl.uniform1f(progStrand.u.u_pre, preExp);
        gl.uniform1f(progStrand.u.u_wob, wobPh);
        gl.uniform3f(progStrand.u.u_gold, gold[0], gold[1], gold[2]);
        gl.uniform3f(progStrand.u.u_dim, sDimL, sDimR, sDimF);
        gl.uniform1f(progStrand.u.u_floor, heroFloor);
        gl.bindBuffer(gl.ARRAY_BUFFER, strandBuf);
        gl.bufferSubData(gl.ARRAY_BUFFER, 0, strandF, 0, strandFloats);
        gl.bindVertexArray(strandVao);
        for (gi = 0; gi < groups.length; gi++) {
          g = groups[gi];
          if (g.kind === 2 || g.idxCount === 0) { continue; }
          gl.uniform3f(progStrand.u.u_col, g.color[0], g.color[1], g.color[2]);
          gl.uniform1f(progStrand.u.u_kind, g.kind);
          gl.uniform1f(progStrand.u.u_ferrule, g.ferrules ? 1 : 0);
          gl.uniform3f(progStrand.u.u_frill, g.amp * heroScale, g.freq, g.amp > 0 ? 1 : 0);
          gl.uniform1f(progStrand.u.u_len, g.length);
          /* a frilled oral arm with >= LACE_ACROSS columns is drawn as a lace
             curtain (STRAND_VS); A's 4-column arms and every filament are not */
          gl.uniform1f(progStrand.u.u_lace, g.kind === 1 && g.amp > 0 && g.across >= LACE_ACROSS ? 1 : 0);
          gl.drawElements(gl.TRIANGLES, g.idxCount, gl.UNSIGNED_INT, g.idx0 * 4);
        }
      }

      /* the fringe: one instanced draw per fringe group */
      if (fringeActive > 0) {
        gl.useProgram(progFringe.p);
        gl.uniformMatrix4fv(progFringe.u.u_vp, false, mVP);
        gl.uniform3f(progFringe.u.u_pos, heroPX, heroPY, HERO_Z);
        gl.uniformMatrix3fv(progFringe.u.u_m, false, m9);
        gl.uniform1f(progFringe.u.u_wave, pulsePhase / TWO_PI);
        gl.uniform1f(progFringe.u.u_time, cTime);
        gl.uniform1f(progFringe.u.u_waveRest, waveRest);
        gl.uniform1f(progFringe.u.u_contract, (species.motion && species.motion.contraction > 0) ? species.motion.contraction : 0.22);
        gl.uniform3f(progFringe.u.u_vel, heroVX, heroVY, 0);
        gl.uniform1f(progFringe.u.u_scale, heroScale);
        gl.uniform1f(progFringe.u.u_nodes, FRINGE_NODES);
        gl.uniform2f(progFringe.u.u_vpSize, rsW, rsH);
        gl.uniform1f(progFringe.u.u_pxPerUnit, pxPerUnit);
        gl.uniform3f(progFringe.u.u_cam, eyeX, eyeY, eyeZ);
        gl.uniform1f(progFringe.u.u_br, heroBright * 0.9);
        gl.uniform1f(progFringe.u.u_kind, 0);
        gl.uniform1f(progFringe.u.u_ferrule, 0);
        gl.uniform1f(progFringe.u.u_pre, preExp);
        gl.uniform1f(progFringe.u.u_wob, wobPh);
        gl.uniform3f(progFringe.u.u_dim, sDimL, sDimR, sDimF);
        gl.uniform1f(progFringe.u.u_floor, heroFloor);
        gl.bindVertexArray(fringeVao);
        for (gi = 0; gi < groups.length; gi++) {
          g = groups[gi];
          if (g.kind !== 2 || g.active === 0) { continue; }
          /* a second fringe group draws from its own slice of the instance
             buffer (WebGL2 has no base instance) */
          if (g.first > 0) {
            gl.bindBuffer(gl.ARRAY_BUFFER, fringeBuf);
            gl.vertexAttribPointer(2, 4, gl.FLOAT, false, 16, g.first * 16);
          }
          gl.uniform1f(progFringe.u.u_len, g.length);
          gl.uniform2f(progFringe.u.u_w, g.w0, g.w1);
          gl.uniform3f(progFringe.u.u_col, g.color[0], g.color[1], g.color[2]);
          gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, FRINGE_NODES * 2, g.active);
          if (g.first > 0) { gl.vertexAttribPointer(2, 4, gl.FLOAT, false, 16, 0); }
        }
      }
      gl.bindVertexArray(null);
      gl.disable(gl.BLEND);
    }

    /* 2. SCENE at render scale: the water blit (bilinear upsample), then the
       creature, into the MSAA buffer; resolved into sceneTex. Without MSAA
       (Low) it renders straight into sceneTex. */
    function drawScene() {
      gl.bindFramebuffer(gl.FRAMEBUFFER, sceneMs ? sceneMs.fbo : sceneFbo);
      gl.viewport(0, 0, rsW, rsH);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.disable(gl.BLEND);
      gl.useProgram(progBlit.p);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, waterTex);
      gl.bindVertexArray(triVao);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      drawCreature();
      if (sceneMs) {
        gl.bindFramebuffer(gl.READ_FRAMEBUFFER, sceneMs.fbo);
        gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, sceneFbo);
        gl.blitFramebuffer(0, 0, rsW, rsH, 0, 0, rsW, rsH, gl.COLOR_BUFFER_BIT, gl.NEAREST);
      }
    }

    /* 3. GOD RAYS at quarter scale */
    function drawRays() {
      gl.bindFramebuffer(gl.FRAMEBUFFER, rayFbo);
      gl.viewport(0, 0, rayW, rayH);
      gl.useProgram(progRays.p);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, sceneTex);
      gl.uniform2f(progRays.u.u_light, heroU, 1.05);
      gl.uniform1i(progRays.u.u_samples, raySamples);
      gl.uniform1f(progRays.u.u_decay, rayDecay);
      gl.uniform1f(progRays.u.u_density, RAY_DENSITY);
      gl.uniform1f(progRays.u.u_weight, rayWeight);
      gl.uniform1f(progRays.u.u_exposure, exposure);
      gl.bindVertexArray(triVao);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }

    /* 4. BLOOM: down the chain (threshold at the first level), then up it,
       each level added into the one above; bloomTex[0] holds the result */
    function drawBloom() {
      var i;
      gl.useProgram(progDown.p);
      gl.uniform1f(progDown.u.u_exposure, exposure);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindVertexArray(triVao);
      for (i = 0; i < bloomLevels; i++) {
        gl.bindFramebuffer(gl.FRAMEBUFFER, bloomFbo[i]);
        gl.viewport(0, 0, bloomW[i], bloomH[i]);
        if (i === 0) {
          gl.bindTexture(gl.TEXTURE_2D, sceneTex);
          gl.uniform2f(progDown.u.u_texel, 1 / rsW, 1 / rsH);
        } else {
          gl.bindTexture(gl.TEXTURE_2D, bloomTex[i - 1]);
          gl.uniform2f(progDown.u.u_texel, 1 / bloomW[i - 1], 1 / bloomH[i - 1]);
        }
        gl.uniform1f(progDown.u.u_first, i === 0 ? 1 : 0);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
      }
      gl.useProgram(progUp.p);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ONE);
      for (i = bloomLevels - 2; i >= 0; i--) {
        gl.bindFramebuffer(gl.FRAMEBUFFER, bloomFbo[i]);
        gl.viewport(0, 0, bloomW[i], bloomH[i]);
        gl.bindTexture(gl.TEXTURE_2D, bloomTex[i + 1]);
        gl.uniform2f(progUp.u.u_texel, 1 / bloomW[i + 1], 1 / bloomH[i + 1]);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
      }
      gl.disable(gl.BLEND);
    }

    /* 5. COMPOSITE to the default framebuffer at drawW x drawH */
    function drawComposite() {
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, drawW, drawH);
      gl.useProgram(progComp.p);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, sceneTex);
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, bloomLevels > 0 ? bloomTex[0] : rayTex);
      gl.activeTexture(gl.TEXTURE2);
      gl.bindTexture(gl.TEXTURE_2D, rayTex);
      gl.activeTexture(gl.TEXTURE0);
      gl.uniform2f(progComp.u.u_texel, 1 / drawW, 1 / drawH);
      gl.uniform1f(progComp.u.u_exposure, exposure);
      gl.uniform1f(progComp.u.u_bloomAmt, bloomLevels > 0 ? BLOOM_AMT : 0);
      gl.uniform1f(progComp.u.u_rayAmt, RAY_AMT);
      gl.uniform3f(progComp.u.u_dim, dimL * dprEff, dimR * dprEff, DIM_FEATHER * dprEff);
      gl.uniform1f(progComp.u.u_fxaa, sceneMs ? 0 : 1);
      gl.bindVertexArray(triVao);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      gl.bindVertexArray(null);
      /* leave no target bound on a unit the next frame renders into: the
         feedback-loop check is per sampler, but two unbinds cost nothing */
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, null);
      gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, null);
      gl.activeTexture(gl.TEXTURE0);
    }

    function renderFrame(dt, tMs) {
      sim(dt, tMs);
      drawWater(); glCheck('water');
      drawScene(); glCheck('scene');
      drawRays(); glCheck('rays');
      drawBloom(); glCheck('bloom');
      drawComposite(); glCheck('composite');
    }

    /* --------------------------------------------------------------- loop */
    function frame(t) {
      rafId = 0;
      if (!running || contextLost) { return; }
      /* RAF-to-RAF wall time is what the tier system measures, clamped so a
         stall (a modal, a GC, a throttled tab) is one slow frame, not a
         verdict; the RAF stamp can precede the performance.now() of start() */
      var wallMs = t - lastT;
      lastT = t;
      if (wallMs > 250) { wallMs = 250; }
      if (wallMs < 0) { wallMs = 0; }
      var dt = wallMs / 1000;
      if (dt > 0.05) { dt = 0.05; }   /* a stalled tab must not leap the sim */
      tierTick(wallMs);
      if (!running) { return; }       /* the tier change failed: fatal() stopped the loop */
      timeS += dt;
      frames++;
      renderFrame(dt, t);
      rafId = requestAnimationFrame(frame);
    }

    function start() {
      if (running || !mounted || dead || contextLost) { return; }
      running = true;
      lastT = nowMs();   /* the first frame after a pause must not integrate the pause */
      if (!rafId) { rafId = requestAnimationFrame(frame); }
    }

    function stop() {
      running = false;
      if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    }

    /* ------------------------------------------------------ context loss */
    function fatal() {
      dead = true;
      stop();
      clearLostTimer();
      if (typeof api.onFatal === 'function') { api.onFatal(); }
    }

    /* A lost context the browser never restores (Chrome blocks WebGL for the
       page after repeated GPU resets; some mobile drivers simply never answer)
       would leave the water blank for the rest of the session, so a loss arms
       a deadline: 3 s of VISIBLE time, then the engine declares itself dead
       and the selector demotes to 2D on a fresh canvas. Browsers routinely
       drop a backgrounded tab's context and hand it back once the tab is
       shown, so hidden time must not count: a 250 ms poll adds only the spans
       whose both ends saw the page visible. */
    function armLostTimer() {
      if (lostTimer || !contextLost || dead || !mounted) { return; }
      lostVisibleMs = 0;
      lostTick = 0;
      lostTimer = setInterval(lostPoll, LOST_POLL_MS);
    }

    function lostPoll() {
      if (!contextLost || dead || !mounted) { clearLostTimer(); return; }
      if (document.hidden) { lostTick = 0; return; }
      var t = nowMs();
      if (lostTick) { lostVisibleMs += t - lostTick; }
      lostTick = t;
      if (lostVisibleMs >= LOST_GRACE_MS) { clearLostTimer(); fatal(); }
    }

    function clearLostTimer() {
      if (lostTimer) { clearInterval(lostTimer); lostTimer = 0; }
    }

    function onCtxLost(e) {
      /* without preventDefault the browser never fires webglcontextrestored */
      if (e && typeof e.preventDefault === 'function') { e.preventDefault(); }
      contextLost = true;
      stop();
      /* every GL object died with the context and a restore does NOT revive
         them; forget the set now so nothing is ever deleted on the wrong
         context. Nothing reads them while contextLost holds: the loop is
         stopped. onCtxRestored rebuilds the full set. */
      res.forget();
      armLostTimer();
    }

    function onCtxRestored() {
      /* a restore landing after the deadline (or after detach) must not
         resurrect this engine on a canvas the selector has already thrown
         away — two render loops would then fight over the page */
      if (dead || !mounted) { return; }
      clearLostTimer();
      contextLost = false;
      try {
        /* extension objects and limits belong to the old context: re-detect */
        detectCaps();
        computeSize();
        buildAll();
        applyTier(tier);   /* the same tier: its targets and meshes died with the context */
      } catch (err) { fatal(); return; }
      /* a hidden document starts later, from onVisibility */
      if (!document.hidden) { start(); }
    }

    /* ---------------------------------------------------------- listeners */
    function onVisibility() {
      if (document.hidden) { stop(); }
      else if (!contextLost) { start(); }
    }

    function onResize() {
      /* debounced: a drag-resize fires dozens of events and each size change
         rebuilds every render target */
      if (resizeTimer) { clearTimeout(resizeTimer); }
      resizeTimer = setTimeout(function () {
        resizeTimer = 0;
        if (!mounted || dead || contextLost) { return; }   /* restore re-sizes anyway */
        /* sizeTargets() releases the old targets before it builds the new,
           so a failure here (an incomplete framebuffer at the new size) must
           end the engine, not leave the loop drawing into nothing */
        try { computeSize(); sizeTargets(); relayout(); } catch (err) { fatal(); }
      }, RESIZE_MS);
    }

    /* pointer: drives the +-3 degree parallax and the cursor light (the
       canvas itself is pointer-events: none, so the window is the target) */
    function onMouseMove(e) {
      mouseNX = (e.clientX / Math.max(1, cssW)) * 2 - 1;
      mouseNY = 1 - (e.clientY / Math.max(1, cssH)) * 2;
      mouseIn = true;
      lastMouseT = nowMs();
    }

    function onMouseOut(e) {
      if (!e.relatedTarget) { mouseIn = false; }
    }

    function addListeners() {
      canvas.addEventListener('webglcontextlost', onCtxLost, false);
      canvas.addEventListener('webglcontextrestored', onCtxRestored, false);
      window.addEventListener('resize', onResize);
      window.addEventListener('mousemove', onMouseMove, { passive: true });
      window.addEventListener('mouseout', onMouseOut, { passive: true });
      document.addEventListener('visibilitychange', onVisibility);
    }

    function removeListeners() {
      if (canvas) {
        canvas.removeEventListener('webglcontextlost', onCtxLost, false);
        canvas.removeEventListener('webglcontextrestored', onCtxRestored, false);
      }
      window.removeEventListener('resize', onResize);
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseout', onMouseOut);
      document.removeEventListener('visibilitychange', onVisibility);
    }

    /* Free every GPU object and let go of the context. Nulling `gl` alone is
       not enough: each object pins its context, and the context pins the
       canvas and its full-screen drawing buffer, so a demoted engine would
       keep the whole water column resident for the session. WEBGL_lose_context
       hands the GPU memory back now instead of at some future GC. The
       listeners go first, so the loss this triggers never reaches onCtxLost.
       The extension is re-fetched here: a restore invalidates the old object. */
    function teardown() {
      stop();
      clearLostTimer();
      if (resizeTimer) { clearTimeout(resizeTimer); resizeTimer = 0; }
      removeListeners();
      res.releaseAll();
      if (gl) {
        try {
          if (!gl.isContextLost()) {
            var loseExt = gl.getExtension('WEBGL_lose_context');
            if (loseExt) { loseExt.loseContext(); }
          }
        } catch (_) { /* a context mid-loss: nothing left to free */ }
      }
      gl = null;
      canvas = null;
      mounted = false;
      contextLost = false;
    }

    /* ----------------------------------------------------------------- API */
    /* mount() returns false on ANY setup failure (no WebGL2, a shader that
       fails to compile or link, a dead engine) so the selector can fall
       through to the next renderer instead of the page breaking. */
    function mount(canvasEl) {
      if (dead) { return false; }
      if (mounted) { return true; }
      species = pickSpecies();
      if (!species || !canvasEl || typeof canvasEl.getContext !== 'function') { return false; }
      /* the contract's load-bearing parts: no bell GLSL, no margin mirror or
         no extent means nothing to draw or place, and no apexY nowhere to
         hang the apex star — fall through */
      if (!species.bell || typeof species.bell.glsl !== 'string' || !species.bell.glsl ||
          typeof species.marginJS !== 'function' || !species.extent || !species.palette ||
          !(species.breath > 0) || !(species.apexY > 0)) { return false; }
      try {
        gl = canvasEl.getContext('webgl2', { alpha: false, antialias: false, depth: false, stencil: false,
          premultipliedAlpha: false, powerPreference: 'high-performance' });
      } catch (_) { gl = null; }
      if (!gl) { return false; }
      canvas = canvasEl;
      mounted = true;
      contextLost = false;
      try {
        detectCaps();
        computeSize();
        initCreature();    /* the CPU arrays buildAll() sizes its buffers from */
        buildAll();
        applyTier(startingTier());
      } catch (err) {
        /* the context and any half-built objects go back with it, so the
           selector's fresh canvas is the only one holding GPU memory */
        teardown();
        return false;
      }
      addListeners();
      if (!document.hidden) { start(); }
      return true;
    }

    /* the old 3D engine's pulse() exactly: sonar shells (<= 6, the most-expired slot is
       recycled), the flash when s >= 2, the mote ripple, and the creature's
       answer — flare, whip kink, a spring impulse away from the origin and a
       kick into every chain node. Zero allocation. */
    function pulse(clientX, clientY, strength) {
      if (!mounted || dead || contextLost) { return; }
      var s = (typeof strength === 'number' && isFinite(strength)) ? strength : 1;
      var x = typeof clientX === 'number' ? clientX : cssW * 0.5;
      var y = typeof clientY === 'number' ? clientY : cssH * 0.5;
      var nx = (x / Math.max(1, cssW)) * 2 - 1;
      var ny = 1 - (y / Math.max(1, cssH)) * 2;
      computeRay(nx, ny);
      var t = urz < -0.001 ? (PULSE_Z - eyeZ) / urz : -PULSE_Z;
      var px = eyeX + urx * t, py = eyeY + ury * t, pz = eyeZ + urz * t;
      computeRay(mouseNX, mouseNY);   /* restore the cursor scratch ray */
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

      /* the light answers first: a ripple of brightness through nearby motes */
      ripX = px; ripY = py; ripZ = pz;
      ripR = 0.6;
      ripA = Math.min(1, 0.4 + 0.28 * Math.min(s, 3));

      /* the Medusa answers: spring impulse + brightness flare + tentacle whip */
      var sc = Math.min(s, 3);
      flare = Math.min(1.5, flare + 0.45 * sc);
      kink = Math.min(1.2, kink + 0.45 * sc);   /* arms the zigzag whip term */
      var hdx = heroX - px, hdy = heroY - py;
      var hdd = Math.sqrt(hdx * hdx + hdy * hdy) + 0.001;
      var himp = sc * 0.55 / (1 + hdd * 0.22);
      heroVX += hdx / hdd * himp;
      heroVY += hdy / hdd * himp;
      var ci, kk;
      for (ci = 0; ci < chainsActive; ci++) {
        var off2 = chOff[ci], len2 = chLen[ci];
        for (kk = 1; kk < len2; kk++) {
          var q3 = (off2 + kk) * 3;
          var qx = ndPos[q3] - px, qy = ndPos[q3 + 1] - py, qz = ndPos[q3 + 2] - pz;
          var qd = Math.sqrt(qx * qx + qy * qy + qz * qz) + 0.001;
          var kick = sc * 0.19 * heroScale * (kk / (len2 - 1)) / (1 + qd * 0.18);
          ndPrev[q3] -= qx / qd * kick;
          ndPrev[q3 + 1] -= qy / qd * kick;
          ndPrev[q3 + 2] -= qz / qd * kick;
          /* alternating lateral component so the whip zigzags immediately
             instead of the whole chain translating as one */
          var zig = kick * 0.6 * Math.sin(kk * 2.4 + ci * 1.9);
          ndPrev[q3] -= (-qy / qd) * zig;
          ndPrev[q3 + 1] -= (qx / qd) * zig;
        }
      }
    }

    /* the 2D engine's semantics: a clamped 0..1 target the sim eases toward; it
       drives the heavens' surge, the mote brightness and the hero glow */
    function setActivity(level) {
      var v = (typeof level === 'number' && isFinite(level)) ? level : 0;
      activityTarget = v < 0 ? 0 : (v > 1 ? 1 : v);
    }

    function detach() {
      teardown();
    }

    function info() {
      /* read-only; the sized fields are null until mount() has measured */
      return { species: species ? species.id : null, tier: mounted ? tier : null, dpr: mounted ? dprEff : null,
               drawW: mounted ? drawW : null, drawH: mounted ? drawH : null,
               renderScale: mounted ? TIERS[tier].scale : null,
               hdr: mounted ? caps.hdr : null, frames: frames,
               /* the bell's CSS-px bounds; a fresh object per call (this is a
                  test hook, not a frame path) */
               heroBox: (mounted && heroInited) ? { x: hbX, y: hbY, w: hbW, h: hbH } : null };
    }

    return api;
  }

  window.JellyfieldHD = { create: createJellyfieldHD, species: SPECIES, params: PARAMS };
})();
