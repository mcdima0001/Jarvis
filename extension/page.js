/**
 * Что Jarvis умеет делать внутри страницы.
 *
 * Здесь лежат две функции, которые расширение впрыскивает во вкладку через
 * `chrome.scripting.executeScript({ func })`. Впрыскивается именно **код этого
 * файла**, а из Jarvis приходят только данные: список шагов и слов. Это
 * принципиально. Голос — недоверенный ввод: он проходит путь микрофон →
 * Whisper → LLM → аргумент, и превращать его в исполняемый код нельзя ни на
 * одном шаге. Селектор уходит в `querySelector`, подпись — в сравнение строк;
 * ни то, ни другое ничего выполнить не может.
 *
 * Обе функции обязаны быть самодостаточными: наружу отправляется их исходный
 * текст, поэтому ничего из окружения расширения внутри не существует.
 *
 * План — это список **вариантов**, а не последовательность. Выполняется первый
 * сработавший: сначала точный селектор конкретного сайта, потом кнопка по
 * подписи, потом сам плеер. Разметку сайты меняют регулярно, поэтому запасной
 * вариант тут не роскошь, а условие того, что команда переживёт редизайн.
 */

/**
 * Выполнить план в текущей странице.
 *
 * @param {Array<Object>} plan шаги-варианты: {media}, {label}, {click}.
 * @returns {Promise<Object>} что сработало: {done, detail, title, url}.
 */
async function jarvisRunPlan(plan) {
  const MAX_STEPS = 8;
  const CLICKABLE =
    'button, [role="button"], [role="menuitem"], [role="switch"], a[href], ' +
    'input[type="button"], input[type="submit"]';

  /** Видно ли элемент. Нулевой размер и `display: none` — это не кнопка. */
  const seen = (element) => {
    if (!element || !element.getBoundingClientRect) {
      return false;
    }
    const box = element.getBoundingClientRect();
    if (!box.width || !box.height) {
      return false;
    }
    const style = getComputedStyle(element);
    return style.visibility !== "hidden" && style.display !== "none" && style.opacity !== "0";
  };

  /**
   * Плееры страницы. Видимость тут не проверяется намеренно: Яндекс Музыка
   * играет через <audio>, у которого размера нет вовсе.
   */
  const players = () =>
    Array.from(document.querySelectorAll("video, audio")).filter(
      (item) => item.readyState > 0 || item.currentTime > 0 || item.duration > 0,
    );

  /** Тот плеер, о котором говорят: играющий, а из молчащих — самый длинный. */
  const mainPlayer = () => {
    const all = players();
    const playing = all.filter((item) => !item.paused && !item.ended);
    const pool = playing.length ? playing : all;
    return pool.sort((first, second) => (second.duration || 0) - (first.duration || 0))[0] || null;
  };

  /** Ёлки-палки регистра и «ё»: сравнивать подписи иначе бессмысленно. */
  const norm = (text) => String(text || "").toLowerCase().replace(/ё/g, "е").replace(/\s+/g, " ").trim();

  /** Подпись кнопки так, как её читает человек и озвучивает экранный диктор. */
  const caption = (element) =>
    norm(element.getAttribute("aria-label") || element.getAttribute("title") || element.textContent);

  const media = async (what, step) => {
    const playing = players().filter((item) => !item.paused && !item.ended);

    if (what === "pause" || (what === "toggle" && playing.length)) {
      if (!playing.length) {
        return null;
      }
      playing.forEach((item) => item.pause());
      return "пауза";
    }

    if (what === "play" || what === "toggle") {
      const target = mainPlayer();
      if (!target) {
        return null;
      }
      try {
        // Браузер вправе отказать: автозапуск со звуком разрешён только там,
        // где пользователь уже что-то включал руками. Отказ — не ошибка, а
        // повод попробовать следующий вариант, то есть нажать кнопку сайта.
        await target.play();
      } catch (error) {
        return null;
      }
      return "воспроизведение";
    }

    const target = mainPlayer();
    if (!target) {
      return null;
    }

    if (what === "mute" || what === "unmute") {
      target.muted = what === "mute";
      return what === "mute" ? "звук выключен" : "звук включён";
    }

    if (what === "louder" || what === "quieter") {
      const step_ = Number(step.amount) > 0 ? Number(step.amount) : 0.1;
      const shift = what === "louder" ? step_ : -step_;
      target.muted = false;
      target.volume = Math.min(1, Math.max(0, target.volume + shift));
      return `громкость ${Math.round(target.volume * 100)}%`;
    }

    if (what === "forward" || what === "back") {
      const seconds = Number(step.seconds) > 0 ? Number(step.seconds) : 15;
      const shift = what === "forward" ? seconds : -seconds;
      const limit = target.duration || Number.MAX_SAFE_INTEGER;
      target.currentTime = Math.min(limit, Math.max(0, target.currentTime + shift));
      return `${Math.round(target.currentTime)} с`;
    }

    return null;
  };

  /**
   * Подпись, разложенная по словам: всё, кроме букв и цифр, становится
   * границей. Иначе кавычки в «Поставить отметку "Нравится"» приклеиваются к
   * слову, и совпадения по нему не находится вовсе.
   */
  const spaced = (text) => ` ${norm(text).replace(/[^\p{L}\p{N}]+/gu, " ").trim()} `;

  /**
   * Совпадение началом слова, а не любым куском. «like» внутри «dislike» и
   * «нравится» внутри «не нравится» — ровно противоположные команды.
   */
  const hits = (label, word) => label.includes(` ${word}`);

  /** Кнопка, чья подпись содержит одно из слов и ни одного запретного. */
  const byLabel = (step) => {
    const wanted = step.label.map((word) => spaced(word).trim()).filter(Boolean);
    const forbidden = (step.avoid || []).map((word) => spaced(word).trim()).filter(Boolean);
    if (!wanted.length) {
      return null;
    }
    for (const element of document.querySelectorAll(CLICKABLE)) {
      if (!seen(element)) {
        continue;
      }
      const caption_ = caption(element);
      // Слишком длинная подпись — это не кнопка, а кусок текста со ссылкой.
      if (!caption_ || caption_.length > 120) {
        continue;
      }
      const label = spaced(caption_);
      // Запрет сильнее совпадения: «не нравится» содержит «нравится» целиком,
      // и без этой проверки лайк превращается в дизлайк.
      if (forbidden.some((word) => hits(label, word))) {
        continue;
      }
      if (wanted.some((word) => hits(label, word))) {
        return element;
      }
    }
    return null;
  };

  /**
   * Включить названное: трек в списке, ролик в подборке.
   *
   * Нажатие по строке трека воспроизведение не запускает — оно её только
   * выбирает или уводит на страницу трека. Играть начинает отдельная кнопка,
   * и она обычно появляется лишь при наведении мыши. Поэтому: находим строку
   * по названию, наводимся на неё, ищем внутри кнопку воспроизведения, а если
   * её нет — нажимаем саму строку и дожимаем плеер.
   */
  const playItem = async (step) => {
    const ITEMS =
      'a[href], button, [role="button"], [role="row"], [role="listitem"], li, tr';
    const wanted = (step.item || []).map((word) => spaced(word).trim()).filter(Boolean);
    const hints = (step.hint || []).map((word) => spaced(word).trim()).filter(Boolean);
    if (!wanted.length) {
      return null;
    }

    let found = null;
    for (const element of document.querySelectorAll(ITEMS)) {
      if (!seen(element)) {
        continue;
      }
      const label = spaced(caption(element));
      // Строка длиннее этого — уже кусок страницы, а не название.
      if (!label.trim() || label.length > 200) {
        continue;
      }
      if (wanted.some((word) => hits(label, word))) {
        found = element;
        break;
      }
    }
    if (!found) {
      return null;
    }

    // Кнопка воспроизведения прячется до наведения, поэтому видимость у неё
    // не проверяем — иначе её не найти никогда.
    let node = found;
    for (let depth = 0; node && depth < 5; depth += 1) {
      for (const name of ["pointerover", "mouseover", "mouseenter"]) {
        try {
          node.dispatchEvent(new MouseEvent(name, { bubbles: true }));
        } catch (error) {
          // Событие не прошло — не страшно, кнопка может быть и так видна.
        }
      }
      const button = Array.from(node.querySelectorAll(CLICKABLE)).find((item) =>
        hints.some((word) => hits(spaced(caption(item)), word)),
      );
      if (button) {
        button.click();
        return `${caption(found)} — ${caption(button)}`;
      }
      node = node.parentElement;
    }

    found.click();
    if (step.play) {
      // Нажатие могло только выбрать трек. Даём странице мгновение и, если
      // тишина, включаем плеер сами.
      await new Promise((done) => setTimeout(done, 400));
      const playing = players().some((item) => !item.paused && !item.ended);
      if (!playing) {
        const target = mainPlayer();
        if (target) {
          try {
            await target.play();
          } catch (error) {
            // Автозапуск запрещён — но строку мы всё-таки нажали.
          }
        }
      }
    }
    return caption(found);
  };

  /** Первый видимый элемент по списку селекторов — вместе с самим селектором. */
  const bySelector = (selectors) => {
    for (const selector of selectors) {
      let found = null;
      try {
        found = document.querySelector(String(selector));
      } catch (error) {
        continue; // селектор из памяти мог устареть или оказаться кривым
      }
      if (found && seen(found)) {
        return { element: found, selector: String(selector) };
      }
    }
    return null;
  };

  // `sel` возвращается, чтобы нажатое можно было потом назвать по имени: если
  // нажалось не то, Jarvis запомнит это как «сюда больше не надо».
  const answer = (done, detail, sel = "") => ({
    done,
    detail,
    sel,
    title: document.title,
    url: location.href,
  });

  for (const step of (plan || []).slice(0, MAX_STEPS)) {
    if (!step || typeof step !== "object") {
      continue;
    }
    try {
      if (typeof step.media === "string") {
        const detail = await media(step.media, step);
        if (detail) {
          return answer("media", detail);
        }
      } else if (Array.isArray(step.item)) {
        const detail = await playItem(step);
        if (detail) {
          return answer("item", detail);
        }
      } else if (Array.isArray(step.label)) {
        const element = byLabel(step);
        if (element) {
          element.click();
          return answer("label", caption(element));
        }
      } else if (Array.isArray(step.click)) {
        const found = bySelector(step.click);
        if (found) {
          found.element.click();
          return answer(
            "click",
            found.element.getAttribute("aria-label") || caption(found.element),
            found.selector,
          );
        }
      }
    } catch (error) {
      // Один вариант не вышел — это ожидаемо, пробуем следующий.
    }
  }

  return answer(null, "");
}

/**
 * Перечислить кнопки страницы: подпись плюс селектор.
 *
 * Нужно ровно для одного случая — когда ни один известный вариант не сработал
 * и решение приходится спрашивать у языковой модели. Поэтому список короткий и
 * подписи урезаны: это одна платная реплика, и платить за неё второй раз для
 * того же сайта уже не придётся — выбор уходит в память.
 *
 * @param {number} limit сколько кнопок вернуть.
 * @returns {Object} {controls: [{name, sel}], title, url}
 */
function jarvisProbe(limit) {
  const MAX_NAME = 60;
  const CLICKABLE = 'button, [role="button"], [role="switch"], [role="menuitem"]';
  const STABLE = ["data-test-id", "data-testid", "data-l", "aria-label", "name"];

  const seen = (element) => {
    const box = element.getBoundingClientRect();
    if (!box.width || !box.height) {
      return false;
    }
    const style = getComputedStyle(element);
    return style.visibility !== "hidden" && style.opacity !== "0";
  };

  /** Селектор, который переживёт перезагрузку страницы. */
  const selectorFor = (element) => {
    if (element.id && /^[A-Za-z][\w-]*$/.test(element.id)) {
      return `#${element.id}`;
    }
    for (const attribute of STABLE) {
      const value = element.getAttribute(attribute);
      // Кавычка внутри значения сломала бы селектор — такие пропускаем.
      if (!value || value.includes('"') || value.length > 80) {
        continue;
      }
      const selector = `[${attribute}="${value}"]`;
      try {
        if (document.querySelectorAll(selector).length === 1) {
          return selector;
        }
      } catch (error) {
        continue;
      }
    }
    return "";
  };

  const controls = [];
  const known = new Set();
  for (const element of document.querySelectorAll(CLICKABLE)) {
    if (controls.length >= (limit || 40)) {
      break;
    }
    if (!seen(element)) {
      continue;
    }
    const name = String(
      element.getAttribute("aria-label") || element.getAttribute("title") || element.textContent || "",
    )
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, MAX_NAME);
    const selector = selectorFor(element);
    if (!name || known.has(name.toLowerCase())) {
      continue;
    }
    known.add(name.toLowerCase());
    controls.push({ name, sel: selector });
  }

  return { controls, title: document.title, url: location.href };
}
