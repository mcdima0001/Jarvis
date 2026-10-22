/**
 * page.js на сохранённой странице: что он найдёт и что нажмёт.
 *
 * Самопроверка (`selftest.js`) строит страницы руками, и они такие, какими мы
 * их себе представляем. Здесь — какими их сделал сайт: владелец сохраняет
 * страницу (Ctrl+S), её копия ложится в `pages/`, и план из настоящего кода
 * скилла выполняется на ней в jsdom. 22.10.2026 так нашлись бы обе ошибки
 * вечера: подсказка «возможно, вы искали», принятая за трек, и «Моя волна»
 * над лентой «Мне нравится».
 *
 * Раскладки jsdom не считает, и для page.js всё было бы невидимым, поэтому у
 * элементов есть размер. Таймеры срабатывают сразу: ждать, пока заиграет звук,
 * на сохранённой странице нечего. Нажатия не исполняются, а записываются.
 *
 * Запуск: node run.js <страница.html> <адрес> '<план JSON>'
 * Ответ — JSON: {result, clicks: [{label, classes, href}]}.
 */

"use strict";

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

async function main() {
  const [file, url, planText] = process.argv.slice(2);
  const html = fs.readFileSync(file, "utf8");
  const dom = new JSDOM(html, { url, runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;

  window.Element.prototype.getBoundingClientRect = () => ({
    width: 100, height: 20, top: 0, left: 0, right: 100, bottom: 20, x: 0, y: 0,
  });
  window.setTimeout = (body) => {
    body();
    return 0;
  };

  const clicks = [];
  const describe = (element) => ({
    label: (element.getAttribute("aria-label") || element.textContent || "").trim().slice(0, 80),
    classes: String(element.className || ""),
    href: element.getAttribute("href") || "",
    // Кто вокруг: по классам предков видно, в каком блоке нажали.
    within: (() => {
      const names = [];
      for (let node = element.parentElement; node && names.length < 12; node = node.parentElement) {
        if (node.className) names.push(String(node.className));
      }
      return names.join(" ");
    })(),
  });
  window.HTMLElement.prototype.click = function click() {
    clicks.push(describe(this));
  };

  const source = fs.readFileSync(path.join(__dirname, "..", "page.js"), "utf8");
  window.eval(`${source}\n;window.__jarvis = { jarvisRunPlan };`);
  const result = await window.__jarvis.jarvisRunPlan(JSON.parse(planText));
  process.stdout.write(JSON.stringify({ result, clicks }));
  window.close();
}

main().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error));
  process.exit(1);
});
