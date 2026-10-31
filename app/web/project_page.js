// The project page: the two reading depths and the live request.
//
// Without this script the page shows everything and offers neither control,
// which is why both start hidden in the markup.
(() => {
  const main = document.querySelector("main");
  const lens = document.getElementById("lens");
  const actions = document.getElementById("trace-actions");
  const result = document.getElementById("trace-result");

  const STORED = "project-lens";
  const STEP_MS = 220;

  function remembered() {
    try {
      return localStorage.getItem(STORED);
    } catch {
      return null;
    }
  }

  function remember(value) {
    try {
      localStorage.setItem(STORED, value);
    } catch {
      // A private window keeps the choice for this page view only.
    }
  }

  function setLens(value) {
    main.dataset.lens = value;
    lens.querySelectorAll("button").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.lens === value));
    });
  }

  if (lens) {
    setLens(remembered() === "deep" ? "deep" : "brief");
    lens.hidden = false;
    lens.addEventListener("click", (event) => {
      const value = event.target.dataset.lens;
      if (value) {
        setLens(value);
        remember(value);
      }
    });
  }

  // Where the moving dot passes on its way through each step, in the drawing's
  // own coordinates.
  const PATH = {
    web: [[560, 76], [560, 138], [380, 138]],
    telegram: [[205, 76], [205, 138], [380, 138]],
    identity: [[380, 207]],
    dispatcher: [[380, 287]],
    "to-plugin": [[380, 338]],
    "page-route": [[100, 207], [100, 330], [479, 330], [479, 342]],
    postgres: [[380, 400], [380, 488]],
    notify: [[380, 410], [638, 410], [638, 488]],
  };
  ["system", "notes", "reminders", "clipboard", "expenses", "applications", "todo"].forEach(
    (name, index) => {
      PATH[`plugin-${name}`] = [[79 + index * 100, 364]];
    },
  );

  const dot = document.getElementById("trace-dot");
  const place = ([x, y]) => `translate(${x}px, ${y}px)`;

  function mark(step, name) {
    document.querySelectorAll(`[data-step~="${step}"]`).forEach((node) => {
      node.classList.add(name);
    });
  }

  function reset() {
    document.querySelectorAll(".lit, .stop").forEach((node) => {
      node.classList.remove("lit", "stop");
    });
    dot.classList.remove("stopped");
    dot.style.opacity = 0;
  }

  async function walk(steps) {
    let from = PATH[steps[0]][0];
    dot.style.transform = place(from);
    dot.style.opacity = 1;

    for (const step of steps) {
      for (const to of PATH[step]) {
        await dot.animate(
          [{ transform: place(from) }, { transform: place(to) }],
          { duration: STEP_MS, easing: "ease-in-out", fill: "forwards" },
        ).finished;
        from = to;
      }
      mark(step, "lit");
    }
  }

  async function run(button, buttons) {
    buttons.forEach((each) => {
      each.disabled = true;
    });
    reset();
    result.textContent = "";

    try {
      const scenario = encodeURIComponent(button.dataset.scenario);
      const response = await fetch(`/project/trace?scenario=${scenario}`);

      if (response.status === 429) {
        result.textContent = "Слишком много запросов, попробуйте через минуту.";
        return;
      }
      if (!response.ok) {
        throw new Error(String(response.status));
      }

      const trace = await response.json();
      await walk(trace.steps);

      if (!trace.ok) {
        const last = trace.steps[trace.steps.length - 1];
        mark(last, "stop");
        dot.classList.add("stopped");
      } else {
        dot.style.opacity = 0;
      }
      const time = `${trace.ms.toFixed(1).replace(".", ",")} мс`;
      result.textContent = `${trace.reply} · ${time}`;
    } catch {
      reset();
      result.textContent = "Запрос не прошёл, попробуйте ещё раз.";
    } finally {
      buttons.forEach((each) => {
        each.disabled = false;
      });
    }
  }

  if (actions) {
    const buttons = [...actions.querySelectorAll("button")];
    actions.hidden = false;
    buttons.forEach((button) => {
      button.addEventListener("click", () => run(button, buttons));
    });
  }
})();
