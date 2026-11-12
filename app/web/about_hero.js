(() => {
  const T = JSON.parse(document.getElementById("i18n").textContent);
  const report = document.getElementById("report");
  const goal = document.getElementById("goal");

  if (!report || !goal) {
    return;
  }

  const output = document.getElementById("goal-value");
  const verdict = document.getElementById("verdict");
  const rows = [...report.querySelectorAll("tbody tr")].map((row) => {
    const total = Number(row.dataset.total);
    const late = Number(row.dataset.late);
    return {
      row,
      total,
      done: total - late,
      share: (1 - late / total) * 100,
      name: row.querySelector("th").textContent,
    };
  });
  let hovered = null;

  const fill = (template, values) =>
    template.replace(/\{(\w+)\}/g, (_, key) => String(values[key]));

  function render() {
    const target = Number(goal.value);
    output.textContent = `${target}${T.percent}`;
    let passing = 0;

    for (const item of rows) {
      const low = item.share < target;
      item.row.classList.toggle("low", low);
      if (!low) {
        passing += 1;
      }
    }

    verdict.textContent = hovered
      ? fill(T.report_row, { name: hovered.name, done: hovered.done, total: hovered.total })
      : fill(T.report_verdict, { ok: passing, all: rows.length, goal: target });
  }

  for (const item of rows) {
    item.row.addEventListener("pointerenter", () => {
      hovered = item;
      render();
    });
    item.row.addEventListener("pointerleave", () => {
      hovered = null;
      render();
    });
  }

  goal.addEventListener("input", () => {
    report.style.setProperty("--goal", goal.value);
    render();
  });
  report.querySelector(".goal").hidden = false;
  render();
})();
