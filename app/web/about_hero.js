// The hero of /about: scattered service tickets become an SLA report.
//
// Every point is one ticket and the report is drawn from the same counts, so
// nothing on the last frame is a picture of something else. Position is a pure
// function of time rather than a simulation, which is why a skipped frame or a
// resize mid-run costs nothing. The figures are invented, and the page says so.
(() => {
  const canvas = document.getElementById("hero-report");
  const replay = document.getElementById("hero-replay");

  if (!canvas || !replay) {
    return;
  }

  const SERVICES = [
    { name: "Рабочие места", total: 412, late: 14 },
    { name: "Учётные записи", total: 286, late: 15 },
    { name: "Корпоративная почта", total: 198, late: 13 },
    { name: "Отчётность", total: 214, late: 17 },
    { name: "Оборудование", total: 130, late: 19 },
  ];
  const DUPLICATES = 80;
  const TARGET = 0.9;
  const SCRAPS = [
    ["NULL", 0.14, 0.22], ["2026-13-41", 0.62, 0.14], ["статус?", 0.8, 0.46],
    ["дубль", 0.3, 0.58], ["#Н/Д", 0.54, 0.76], ["01.02 или 02.01", 0.08, 0.86],
  ];
  const END = 5.4;
  const MANROPE = "Manrope, sans-serif";
  const MONO = '"JetBrains Mono", monospace';

  const style = getComputedStyle(document.documentElement);
  const color = (name) => style.getPropertyValue(name).trim();
  let INK;
  let MUTED;
  let ON_TIME;
  let LATE;

  function readColors() {
    INK = color("--text");
    MUTED = color("--muted");
    ON_TIME = color("--accent");
    LATE = color("--coral");
  }

  readColors();

  const ctx = canvas.getContext("2d");
  const clamp = (x) => Math.min(1, Math.max(0, x));
  const lerp = (a, b, k) => a + (b - a) * k;
  const ease = (x) => (x < 0.5 ? 4 * x * x * x : 1 - (-2 * x + 2) ** 3 / 2);
  const percent = (s) => ((1 - s.late / s.total) * 100).toFixed(1).replace(".", ",");

  // A fixed seed, so the page opens the same way for everyone.
  let seed = 20231009;
  const random = () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let x = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x;
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
  };

  function shuffled(count) {
    const order = Array.from({ length: count }, (_, i) => i);
    for (let i = count - 1; i > 0; i -= 1) {
      const j = Math.floor(random() * (i + 1));
      [order[i], order[j]] = [order[j], order[i]];
    }
    return order;
  }

  const scattered = () => ({
    x: random(),
    y: random(),
    phase: random() * Math.PI * 2,
    speed: 0.5 + random(),
    delay: random() * 0.5,
  });

  // Cells are filled column by column, on-time tickets first, so the sorted
  // lane has the same split as the bar that replaces it.
  const tickets = [];
  SERVICES.forEach((service, lane) => {
    const rows = 4;
    const columns = Math.ceil(service.total / rows);
    const cell = (i) => ({
      u: (Math.floor(i / rows) + 0.5) / columns,
      v: (i % rows + 0.5) / rows,
    });
    const order = shuffled(service.total);
    for (let i = 0; i < service.total; i += 1) {
      tickets.push({
        ...scattered(),
        lane,
        late: i >= service.total - service.late,
        loose: cell(order[i]),
        sorted: cell(i),
      });
    }
  });
  const duplicates = Array.from({ length: DUPLICATES }, scattered);

  let width = 0;
  let height = 0;
  let plot = {};

  function layout() {
    width = canvas.clientWidth;
    height = Math.max(Math.round(width * 0.74), 300);
    canvas.style.height = `${height}px`;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const top = 62;
    const lane = (height - top - 24) / SERVICES.length;
    plot = { top, lane, bar: Math.min(lane - 24, 14), bottom: top + lane * SERVICES.length };
  }

  const barTop = (lane) => plot.top + plot.lane * lane + 20;

  function text(value, x, y, font, fill, alpha, align = "left") {
    if (alpha <= 0) {
      return;
    }
    ctx.globalAlpha = alpha;
    ctx.font = font;
    ctx.fillStyle = fill;
    ctx.textAlign = align;
    ctx.fillText(value, x, y);
  }

  // The headline says what the picture is at each stage, ending on the finding.
  function headline(t) {
    const stages = [
      [0, 1.3, "1 320 записей из разных систем", "форматы не сходятся, есть дубли"],
      [1.3, 3.9, "1 240 обращений после проверки", "80 дублей отброшено"],
      [3.9, Infinity, "4 из 5 услуг укладываются в SLA", "вымышленный пример · 1 240 обращений · 93,7 % в срок"],
    ];
    for (const [from, to, title, note] of stages) {
      const alpha = clamp((t - from) / 0.3) * clamp((to - t) / 0.2);
      text(title, 0, 16, `600 16px ${MANROPE}`, INK, alpha);
      text(note, 0, 38, `11px ${MONO}`, MUTED, alpha);
    }
  }

  function drawScraps(t) {
    const alpha = 0.55 * clamp(t / 0.4) * clamp((1.5 - t) / 0.4);
    for (const [label, x, y] of SCRAPS) {
      text(label, x * width * 0.8, plot.top + y * (height - plot.top), `10px ${MONO}`, LATE, alpha);
    }
  }

  function drift(p, t) {
    return {
      x: (p.x * 0.96 + 0.02 + 0.02 * Math.sin(t * p.speed + p.phase)) * width,
      y: plot.top + (p.y * 0.96 + 0.02 + 0.03 * Math.cos(t * p.speed + p.phase)) * (height - plot.top - 8),
    };
  }

  function drawTickets(t) {
    const fadeIn = clamp(t / 0.4);
    const fadeOut = 1 - clamp((t - 4) / 0.5);

    if (fadeOut <= 0) {
      return;
    }

    for (const p of tickets) {
      const here = drift(p, t);
      const toLane = ease(clamp((t - 1.2 - p.delay) / 0.9));
      const sort = ease(clamp((t - 2.8 - p.sorted.u * 0.4) / 0.7));
      const y = barTop(p.lane);
      const x1 = lerp(here.x, p.loose.u * width, toLane);
      const y1 = lerp(here.y, y + p.loose.v * plot.bar, toLane);
      const x = lerp(x1, p.sorted.u * width, sort);
      const lift = p.late ? Math.sin(Math.PI * sort) * 8 : 0;
      const sized = lerp(y1, y + p.sorted.v * plot.bar, sort) - lift;
      const tinted = clamp((t - 2.9 - p.sorted.u * 0.4) / 0.4);

      ctx.globalAlpha = fadeIn * fadeOut * (1 - tinted) * 0.8;
      ctx.fillStyle = MUTED;
      ctx.fillRect(x - 1, sized - 1, 2, 2);
      ctx.globalAlpha = fadeIn * fadeOut * tinted;
      ctx.fillStyle = p.late ? LATE : ON_TIME;
      ctx.fillRect(x - 1, sized - 1, 2, 2);
    }

    // The duplicates fall out of the data rather than joining a lane.
    for (const p of duplicates) {
      const gone = clamp((t - 1.1 - p.delay) / 0.6);
      const here = drift(p, t);
      ctx.globalAlpha = fadeIn * (1 - gone) * 0.9;
      ctx.fillStyle = LATE;
      ctx.fillRect(here.x - 1, here.y + gone * gone * 40 - 1, 2, 2);
    }
  }

  function drawReport(t) {
    const labels = clamp((t - 1.5) / 0.5);
    const solid = ease(clamp((t - 3.9) / 0.6));
    const figures = clamp((t - 4.2) / 0.4);

    SERVICES.forEach((service, lane) => {
      const y = barTop(lane);
      const share = 1 - service.late / service.total;
      const missed = share < TARGET;
      text(service.name, 0, y - 6, `12px ${MANROPE}`, INK, labels);
      text(`${percent(service)} %`, width, y - 6, `12px ${MONO}`, INK, figures, "right");
      if (missed) {
        const shift = ctx.measureText(`${percent(service)} %`).width + 10;
        text("▼ ниже цели", width - shift, y - 6, `11px ${MONO}`, MUTED, figures, "right");
      }
      ctx.globalAlpha = solid;
      ctx.fillStyle = ON_TIME;
      ctx.fillRect(0, y, width * share - 1, plot.bar);
      ctx.fillStyle = LATE;
      ctx.fillRect(width * share + 1, y, width * (1 - share) - 1, plot.bar);
    });

    // Row by row rather than one rule down the plot, which would strike
    // through the figures between the bars.
    const x = Math.round(width * TARGET) + 0.5;
    ctx.strokeStyle = INK;
    ctx.lineWidth = 1;
    SERVICES.forEach((_, lane) => {
      const y = barTop(lane);
      ctx.globalAlpha = clamp((t - 4.4 - lane * 0.08) / 0.3);
      ctx.beginPath();
      ctx.moveTo(x, y - 3);
      ctx.lineTo(x, y + plot.bar + 3);
      ctx.stroke();
    });

    const y = height - 6;
    ctx.globalAlpha = figures;
    ctx.fillStyle = ON_TIME;
    ctx.fillRect(0, y - 8, 8, 8);
    ctx.fillStyle = LATE;
    ctx.fillRect(66, y - 8, 8, 8);
    ctx.fillStyle = INK;
    ctx.fillRect(166.5, y - 10, 1, 12);
    text("в срок", 12, y, `10px ${MONO}`, MUTED, figures);
    text("просрочено", 78, y, `10px ${MONO}`, MUTED, figures);
    text("цель 90 %", 173, y, `10px ${MONO}`, MUTED, figures);
  }

  function draw(t) {
    ctx.clearRect(0, 0, width, height);
    headline(t);
    drawScraps(t);
    drawTickets(t);
    drawReport(t);
    ctx.globalAlpha = 1;
  }

  let frame = 0;
  let started = null;

  function tick(now) {
    // From the first painted frame, not from the load: a tab opened behind
    // another gets no frames, and would otherwise wake up on the last one.
    started ??= now;
    const t = (now - started) / 1000;
    draw(Math.min(t, END));
    frame = t < END ? requestAnimationFrame(tick) : 0;
  }

  function play() {
    cancelAnimationFrame(frame);
    started = null;
    frame = requestAnimationFrame(tick);
  }

  replay.addEventListener("click", play);
  replay.hidden = false;

  document.addEventListener("themechange", () => {
    readColors();
    if (!frame && width) {
      draw(END);
    }
  });

  const faces = [
    document.fonts.load(`600 16px ${MANROPE}`, "Яя"),
    document.fonts.load(`11px ${MONO}`, "Яя"),
  ];
  Promise.allSettled(faces).then(() => {
    layout();
    // Plays whatever prefers-reduced-motion says: a managed work laptop sets it
    // by policy, and that is where much of this page's audience reads it. One
    // short run with no flashing is the accepted cost; see DECISIONS.md.
    play();
    // Watched only from here: before the fonts arrive, a redraw would show the
    // last frame ahead of the first.
    new ResizeObserver(() => {
      layout();
      if (!frame) {
        draw(END);
      }
    }).observe(canvas);
  });
})();
