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
 * @param {Array<Object>} plan шаги-варианты: {media}, {label}, {click}, {item},
 *   {type}, {scroll}.
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

  /** Слова просьбы, по которым есть смысл искать: короткие не значат ничего. */
  const keywords = (text) => text.split(" ").filter((word) => word.length >= 3);

  /**
   * Насколько подпись отвечает просьбе: 1 — просьба нашлась целиком, 0 — нет.
   *
   * Целиком совпадает далеко не всё. Название ролика проходит путь микрофон →
   * Whisper → шаблон фразы, и обрастает по дороге лишними словами: «нажми на
   * видео, как я на топлес обманывал всех 10 лет» против «Как я обманывал ВСЕХ
   * 10 лет | Ян Топлес» на странице. Подряд эти строки не совпадают нигде, а по
   * словам — почти полностью. Поэтому вторым заходом считается доля слов
   * просьбы, найденных в подписи; порог высокий, и одного слова не хватает
   * никогда — иначе «видео» открывало бы первое попавшееся.
   */
  const ENOUGH = 0.6;
  const score = (label, wanted) => {
    if (hits(label, wanted)) {
      return 1;
    }
    const words = keywords(wanted);
    if (words.length < 2) {
      return 0;
    }
    const found = words.filter((word) => hits(label, word)).length;
    const share = found / words.length;
    return found >= 2 && share >= ENOUGH ? share * 0.9 : 0;
  };

  /** Лучшее совпадение подписи с просьбой среди элементов страницы. */
  const bestMatch = (selector, wanted, forbidden, tighter) => {
    const found = [];
    for (const element of document.querySelectorAll(selector)) {
      if (!seen(element)) {
        continue;
      }
      const caption_ = caption(element);
      // Длиннее этого — уже кусок страницы, а не подпись.
      if (!caption_ || caption_.length > 200) {
        continue;
      }
      const label = spaced(caption_);
      // Запрет сильнее совпадения: «не нравится» содержит «нравится» целиком,
      // и без этой проверки лайк превращается в дизлайк.
      if (forbidden.some((word) => hits(label, word))) {
        continue;
      }
      const rank = Math.max(...wanted.map((word) => score(label, word)));
      if (rank > 0) {
        found.push({ element, rank, size: label.length });
      }
    }
    if (!found.length) {
      return null;
    }

    // Обёртка списка склеивает подписи всех строк подряд и поэтому отвечает
    // просьбе **лучше** любой отдельной строки: слов в ней больше. Живой
    // случай на Яндекс Музыке — «включи трек Dua Lipa Break My Heart» выбрало
    // «new rulesdua lipa03:29let you break my heart againmorlix…», то есть всю
    // выдачу целиком. Одной длины тут мало: у обёртки и совпадение выше.
    // Поэтому элемент, внутри которого лежит другой подходящий, отбрасывается —
    // побеждает самый внутренний.
    const inner = found.filter(
      (one) =>
        !found.some(
          (other) =>
            other !== one &&
            typeof one.element.contains === "function" &&
            one.element.contains(other.element),
        ),
    );
    const pool = inner.length ? inner : found;

    // При равном совпадении порядок решает вызывающий: у кнопок побеждает
    // первая на странице (у видео это лайк ролика, а не лайк комментария),
    // у строк списка — самая короткая подпись.
    let best = pool[0];
    for (const item of pool.slice(1)) {
      if (item.rank > best.rank || (tighter && item.rank === best.rank && item.size < best.size)) {
        best = item;
      }
    }
    return best.element;
  };

  /** Кнопка, чья подпись отвечает просьбе и не содержит запретного. */
  const byLabel = (step) => {
    const wanted = step.label.map((word) => spaced(word).trim()).filter(Boolean);
    const forbidden = (step.avoid || []).map((word) => spaced(word).trim()).filter(Boolean);
    if (!wanted.length) {
      return null;
    }
    return bestMatch(CLICKABLE, wanted, forbidden, false);
  };

  /**
   * Пролистать страницу.
   *
   * Кнопки для этого нет ни на одном сайте, а просьба обычная. Живой случай:
   * «пролистай страницу вверх» модель разобрала как перемотку назад — из всего
   * каталога это было самое близкое.
   */
  const scrollPage = (step) => {
    const where = String(step.scroll || "").toLowerCase();
    const height = window.innerHeight || 600;
    const moves = {
      down: [0, height * 0.9],
      up: [0, -height * 0.9],
      top: [0, -1e7],
      bottom: [0, 1e7],
    };
    const move = moves[where];
    if (!move) {
      return null;
    }
    const before = window.scrollY;
    window.scrollBy({ left: move[0], top: move[1], behavior: "instant" });
    // Страница могла быть уже в самом низу — тогда листать было нечего, и это
    // не успех: пусть план идёт дальше, вдруг у сайта своя кнопка.
    return window.scrollY === before && where !== "top" && where !== "bottom" ? null : where;
  };

  /**
   * Напечатать текст в поле на странице — например в поиск сайта.
   *
   * Это **не** эмуляция клавиатуры на уровне системы: текст кладётся в
   * конкретное поле конкретной вкладки и никуда больше уйти не может. Значение
   * ставится через родной сеттер, иначе React с Vue его не замечают: они
   * следят за свойством, а не за атрибутом.
   */
  const typeInto = (step) => {
    const FIELDS =
      'input[type="search"], input[type="text"], input:not([type]), textarea, ' +
      '[role="searchbox"], [role="combobox"] input, [contenteditable="true"]';
    const text = String(step.type == null ? "" : step.type);
    if (!text) {
      return null;
    }

    let field = null;
    // Поле уже в фокусе — печатаем в него: обычно пользователь только что сам
    // нажал «поиск», и открылось именно оно.
    const active = document.activeElement;
    if (active && active.matches && seen(active)) {
      try {
        field = active.matches(FIELDS) ? active : null;
      } catch (error) {
        field = null;
      }
    }
    const where = Array.isArray(step.into) && step.into.length ? step.into : [FIELDS];
    for (const selector of where) {
      if (field) {
        break;
      }
      try {
        field = Array.from(document.querySelectorAll(String(selector))).find(seen) || null;
      } catch (error) {
        continue;
      }
    }
    if (!field) {
      return null;
    }

    if (field.focus) {
      field.focus();
    }
    if (field.isContentEditable) {
      field.textContent = text;
    } else {
      let assigned = false;
      try {
        const proto =
          typeof HTMLTextAreaElement !== "undefined" && field instanceof HTMLTextAreaElement
            ? HTMLTextAreaElement
            : HTMLInputElement;
        const value = Object.getOwnPropertyDescriptor(proto.prototype, "value");
        if (value && value.set) {
          value.set.call(field, text);
          assigned = true;
        }
      } catch (error) {
        assigned = false;
      }
      if (!assigned) {
        field.value = text;
      }
    }
    for (const name of ["input", "change"]) {
      try {
        field.dispatchEvent(new Event(name, { bubbles: true }));
      } catch (error) {
        // Событие не прошло — значение всё равно на месте.
      }
    }

    if (step.submit) {
      for (const name of ["keydown", "keypress", "keyup"]) {
        try {
          field.dispatchEvent(
            new KeyboardEvent(name, {
              key: "Enter",
              code: "Enter",
              keyCode: 13,
              which: 13,
              bubbles: true,
            }),
          );
        } catch (error) {
          // Нажатие не прошло — ниже попробуем отправить форму.
        }
      }
      const form = field.form || (field.closest ? field.closest("form") : null);
      if (form && form.requestSubmit) {
        try {
          form.requestSubmit();
        } catch (error) {
          // Форма отказалась — поле всё равно заполнено, видно на экране.
        }
      }
    }
    return text;
  };

  /**
   * Идёт ли звук прямо сейчас. Успех «включи» измеряется этим, а не нажатием.
   *
   * Спрашивается у **всех** плееров страницы, а не у тех, что прошли фильтр
   * `players()`. Это не мелочь: тот фильтр требует `readyState`, длительности
   * или сдвинутого времени, а свежесозданный плеер ничего этого ещё не имеет —
   * и «уже играет» читалось как тишина. Ровно на этом Jarvis нажимал трек,
   * не верил, что получилось, и шёл тыкать дальше.
   *
   * `muted` тоже считается тишиной: на YouTube наведение мыши запускает беззвучный
   * предпросмотр ролика, а наведение — как раз то, что делает `playItem`.
   */
  const sounding = () =>
    Array.from(document.querySelectorAll("video, audio")).some(
      (item) => !item.paused && !item.ended && !item.muted,
    );

  /**
   * Дождаться звука.
   *
   * Плеер запускается не мгновенно: сайт успевает сходить за ссылкой на файл.
   * Проверять сразу после нажатия бессмысленно — тишина ничего не значит. Но и
   * ждать долго нельзя: расширение держит ответ, а у команды есть свой предел.
   */
  const awaitSound = async (limit) => {
    for (let waited = 0; waited < limit; waited += 100) {
      if (sounding()) {
        return true;
      }
      await new Promise((done) => setTimeout(done, 100));
    }
    return sounding();
  };

  /** Подписи, которыми называют остановку. Кнопка с такой — уже играет. */
  const PAUSE_WORDS = ["пауза", "приостановить", "остановить", "pause", "stop"];

  /**
   * Кнопка воспроизведения превратилась в кнопку паузы.
   *
   * Это ответ самого сайта: «принял, играю». Ждать тут больше нечего, даже если
   * звука ещё нет — трек может грузиться. Без этого признака оставался зазор:
   * Яндекс Музыка сперва идёт за ссылкой на файл, и `paused` переключается не
   * сразу. Не дождавшись, Jarvis шёл нажимать всё подряд — а трек уже играл.
   */
  const saysPlaying = (button) =>
    Boolean(button) && PAUSE_WORDS.some((word) => hits(spaced(caption(button)), word));

  /** Дождаться, что трек пошёл: либо слышно, либо кнопка стала паузой. */
  const awaitStart = async (button, limit) => {
    for (let waited = 0; waited < limit; waited += 100) {
      if (sounding() || saysPlaying(button)) {
        return true;
      }
      await new Promise((done) => setTimeout(done, 100));
    }
    return sounding() || saysPlaying(button);
  };

  /**
   * Что было рядом: подписи и признаки. Это данные для лога, по которым потом
   * пишется рецепт сайта.
   *
   * Из классов берётся один, и не первый попавшийся: сборка склеивает их
   * десятками (`uwk3hf… IlG7b1… HbaqudSq… undefined …`), и такой «селектор»
   * бесполезен. Класс с двойным подчёркиванием — это имя из CSS-модуля
   * (`PlayButtonWithPosition_playButton__7cfDQ`), единственное осмысленное.
   */
  const listOf = (elements) =>
    elements.slice(0, 8).map((item) => {
      const classes = String(item.getAttribute("class") || "").split(/\s+/);
      const telling = classes.find((name) => name.includes("__")) || classes[0] || "";
      return {
        name: caption(item).slice(0, 40),
        sel: item.getAttribute("data-test-id") || telling,
      };
    });

  /**
   * Включить названное: трек в списке, ролик в подборке.
   *
   * Нажатие по строке трека воспроизведение не запускает — оно её только
   * выбирает или уводит на страницу трека. Играть начинает отдельная кнопка,
   * и она обычно появляется лишь при наведении мыши. Поэтому: находим строку
   * по названию, наводимся на неё, ищем внутри кнопку воспроизведения, а если
   * её нет — нажимаем саму строку и дожимаем плеер.
   *
   * **Нажатие — не то же самое, что звук.** Живой случай: на Яндекс Музыке
   * строка нашлась, кнопка «Воспроизведение» нажалась, Jarvis отчитался
   * «включаю» — и тишина. Поэтому с `play: true` успехом считается только
   * появившийся звук: не появился — пробуем следующий способ, а не врём.
   */
  const playItem = async (step) => {
    const ITEMS =
      'a[href], button, [role="button"], [role="row"], [role="listitem"], li, tr';
    const wanted = (step.item || []).map((word) => spaced(word).trim()).filter(Boolean);
    const hints = (step.hint || []).map((word) => spaced(word).trim()).filter(Boolean);
    if (!wanted.length) {
      return null;
    }

    // Верхний результат выдачи, если он назван, важнее сравнения названий:
    // порядок расставил сайт, и знает он о треках больше, чем мы — об
    // услышанном названии. Приходит это только с той выдачи, которую Jarvis
    // открыл сам; на чужой странице `prefer` не присылают.
    //
    // Видимость намеренно не проверяется: кнопка на большой карточке появляется
    // при наведении, и с проверкой её не найти никогда — та же причина, что и у
    // кнопки внутри строки ниже.
    const preferred = (selectors) => {
      for (const selector of selectors) {
        try {
          const node = document.querySelector(String(selector));
          if (node) {
            return node;
          }
        } catch (error) {
          continue; // селектор мог устареть вместе с редизайном
        }
      }
      return null;
    };
    const top = Array.isArray(step.prefer) ? preferred(step.prefer) : null;

    // Иначе побеждает самое тесное совпадение. У списка треков подпись
    // начинается с первого трека, и без этого выбирался весь список целиком:
    // «i need your love oliver nelson & tobtok remix… midnight city…».
    const found = top || bestMatch(ITEMS, wanted, [], true);
    if (!found) {
      // Ничего не совпало. Расскажем, что было **ближе всего** к просьбе, а не
      // что попалось первым: первыми на странице стоят пункты бокового меню
      // («главная; поиск; моя волна…»), и такой список не говорил ни о чём.
      // По ближайшим сразу видно, дело в услышанном названии или выдача ещё не
      // дорисовалась и сравнивать было не с чем.
      // Мера тут своя, без порога: для отчёта важно даже одно общее слово, а
      // для нажатия его мало. С порогом список опять уехал бы в меню — у всех
      // пунктов ноль, а «поиск» ещё и короче остальных.
      const nearness = (label) =>
        Math.max(
          ...wanted.map((word) => {
            if (hits(label, word)) {
              return 1;
            }
            const words = keywords(word);
            return words.length
              ? words.filter((one) => hits(label, one)).length / words.length
              : 0;
          }),
        );

      const near = [];
      for (const element of document.querySelectorAll(ITEMS)) {
        const text = seen(element) ? caption(element) : "";
        if (text && text.length <= 60 && !near.some((item) => item.text === text)) {
          near.push({ text, rank: nearness(spaced(text)) });
        }
      }
      near.sort((first, second) => second.rank - first.rank || first.text.length - second.text.length);
      return { saw: near.slice(0, 8).map((item) => item.text) };
    }

    // Кнопка воспроизведения прячется до наведения, поэтому видимость у неё
    // не проверяем — иначе её не найти никогда. И часто у неё вообще нет
    // подписи: внутри только иконка. Тогда остаётся признак в атрибутах.
    const MARKS = '[data-test-id*="PLAY" i], [class*="play" i], [aria-label*="play" i]';
    let node = found;
    let nearby = [];
    let pressed = "";
    let control = null;
    for (let depth = 0; node && depth < 6; depth += 1) {
      for (const name of ["pointerover", "mouseover", "mouseenter"]) {
        try {
          node.dispatchEvent(new MouseEvent(name, { bubbles: true }));
        } catch (error) {
          // Событие не прошло — не страшно, кнопка может быть и так видна.
        }
      }
      const inside = Array.from(node.querySelectorAll(CLICKABLE));
      if (inside.length) {
        nearby = inside;
      }
      let button = inside.find((item) =>
        hints.some((word) => hits(spaced(caption(item)), word)),
      );
      if (!button) {
        try {
          button = node.querySelector(MARKS);
        } catch (error) {
          button = null;
        }
      }
      if (button) {
        control = button;
        // Кнопка уже показывает паузу — значит этот трек играет прямо сейчас.
        // Нажать её означало бы остановить музыку в ответ на «включи».
        if (step.play && saysPlaying(button)) {
          return { detail: `${caption(found)} — уже играет`, played: true };
        }
        button.click();
        pressed = caption(button) || "кнопка без подписи";
        break;
      }
      node = node.parentElement;
    }

    // У кнопки на большой карточке своей подписи может не быть вовсе, а
    // «воспроизведение — воспроизведение» в логе не говорит ни о чём.
    const name = top ? `верхний результат${caption(found) ? `: ${caption(found)}` : ""}` : caption(found);
    const detail = pressed ? `${name} — ${pressed}` : name;

    // Просили просто нажать — на этом всё, звук тут никто не обещал.
    if (!step.play) {
      if (pressed) {
        return { detail };
      }
      found.click();
      return { detail, buttons: listOf(nearby) };
    }

    // Ждём не загрузку трека, а ответ сайта: либо пошёл звук, либо кнопка
    // превратилась в паузу. Второе не менее надёжно — сайт сам говорит, что
    // принял нажатие, — и закрывает зазор, в котором Яндекс Музыка ещё идёт за
    // ссылкой на файл. Пределы короткие: расширение держит ответ, а с
    // секундными паузами на каждый шаг одна команда занимала 18 с и упиралась
    // в «расширение не ответило».
    if (pressed && (await awaitStart(control, 1200))) {
      return { detail, played: true };
    }

    // Кнопка не помогла или её не было — нажимаем саму строку. Нажатие могло
    // только выбрать трек, поэтому потом дожимаем плеер руками.
    found.click();
    if (await awaitStart(control, 700)) {
      return { detail, played: true };
    }
    const target = mainPlayer();
    if (target) {
      try {
        await target.play();
      } catch (error) {
        // Автозапуск запрещён — но строку мы всё-таки нажали.
      }
    }
    const played = await awaitSound(400);
    // Звука так и нет — расскажем, что было рядом. По этому списку в логе
    // пишется точный рецепт сайта, без угадывания.
    return played ? { detail, played } : { detail, played, buttons: listOf(nearby) };
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

  //: Что видно на странице, если название не нашлось. Уходит в лог Jarvis.
  let missed = null;

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
      } else if (typeof step.scroll === "string") {
        const where = scrollPage(step);
        if (where) {
          return answer("scroll", where);
        }
      } else if (typeof step.type === "string") {
        const typed = typeInto(step);
        if (typed) {
          return answer("type", typed);
        }
      } else if (Array.isArray(step.item)) {
        const done = await playItem(step);
        if (done && done.detail !== undefined) {
          const reply = answer("item", done.detail);
          if (done.buttons && done.buttons.length) {
            reply.buttons = done.buttons;
          }
          if (done.played !== undefined) {
            // Нажали, но звука нет: пусть Jarvis скажет об этом честно, а не
            // отчитается «включаю» в тишину.
            reply.played = done.played;
          }
          return reply;
        }
        if (done && done.saw) {
          missed = done.saw;
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

  const nothing = answer(null, "");
  if (missed) {
    nothing.saw = missed;
  }
  return nothing;
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
