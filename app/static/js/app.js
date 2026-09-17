/*
 * ssavevideo.com — progressive enhancement layer.
 *
 * Design rules, in order of how much they matter:
 *
 * 1. The page is fully usable with JavaScript disabled. The form has a real
 *    `action="/api/extract"` and `method="post"`, so browsers that never run
 *    this file still reach the resolver; JS only upgrades that into an inline
 *    result card with no navigation.
 * 2. Nothing here writes markup. Every value taken from the API (video title,
 *    channel name, labels) is inserted with `textContent`, and every element is
 *    built with `createElement`. A malicious video titled "<img onerror=…>" is
 *    therefore inert, and no `innerHTML` sink exists to be found later.
 * 3. No inline styles and no inline handlers: the CSP is `script-src 'self';
 *    style-src 'self'`, so state changes toggle classes and sizes are declared
 *    in app.css.
 * 4. One abort controller, one request in flight, and results are cached in
 *    sessionStorage per URL, so pressing Enter twice or coming back to a pasted
 *    link is instant.
 */
(function () {
  "use strict";

  var ENDPOINT = "/api/extract";
  var CACHE_TTL_MS = 5 * 60 * 1000;
  var CACHE_KEY = "ssavevideo:results";

  /** Tiny class-based state switcher: the template owns the visuals. */
  function show(el, on) {
    if (!el) return;
    if (on) el.removeAttribute("hidden");
    else el.setAttribute("hidden", "");
  }

  /** Build `tag.class.class` with optional text. Never parses HTML. */
  function el(tag, classes, text) {
    var node = document.createElement(tag);
    if (classes) node.className = classes;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function Store(maxBytes) {
    try {
      this.raw = window.localStorage;
      this.key = CACHE_KEY;
      this.max = maxBytes || 60000;
    } catch (e) {
      this.raw = null;
    }
  }
  Store.prototype.read = function () {
    if (!this.raw) return {};
    try {
      var parsed = JSON.parse(this.raw.getItem(this.key) || "{}");
      var now = Date.now();
      var fresh = {};
      Object.keys(parsed).forEach(function (url) {
        if (parsed[url] && now - parsed[url].at < CACHE_TTL_MS) fresh[url] = parsed[url];
      });
      return fresh;
    } catch (e) {
      return {};
    }
  };
  Store.prototype.write = function (url, data) {
    if (!this.raw) return;
    try {
      var all = this.read();
      all[url] = { at: Date.now(), data: data };
      var json = JSON.stringify(all);
      // Keep the newest entries when a few viral links would overflow the quota.
      while (json.length > this.max) {
        var oldest = Object.keys(all).sort(function (a, b) {
          return all[a].at - all[b].at;
        })[0];
        if (!oldest) break;
        delete all[oldest];
        json = JSON.stringify(all);
      }
      this.raw.setItem(this.key, json);
    } catch (e) {
      /* quota or privacy mode: caching is an optimisation, never a requirement */
    }
  };

  /**
   * Strings come from `data-strings` on the form: a JSON blob rendered by Jinja
   * into an attribute. That keeps the whole app CSP-clean (no inline script to
   * hold the dictionary) and saves a second request per page view.
   */
  function Messages(node) {
    var dict = {};
    try {
      dict = JSON.parse((node && node.getAttribute("data-strings")) || "{}");
    } catch (e) {
      dict = {};
    }
    return {
      t: function (key, fallback) {
        return dict[key] || fallback || "";
      },
    };
  }

  function Boot() {
    var form = document.querySelector("[data-extract-form]");
    if (!form) return;

    var input = form.querySelector("#ss-url");
    var pasteBtn = form.querySelector("[data-paste]");
    var clearBtn = form.querySelector("[data-clear]");
    var submitBtn = form.querySelector("[data-submit]");
    var submitLabel = form.querySelector("[data-submit-label]");
    var submitIcon = form.querySelector("[data-submit-icon]");
    var states = {
      loading: document.querySelector('[data-state="loading"]'),
      error: document.querySelector('[data-state="error"]'),
      result: document.querySelector('[data-state="result"]'),
    };
    var errorMsg = document.querySelector("[data-error-message]");
    var errorHint = document.querySelector("[data-error-hint]");
    var errorDismiss = document.querySelector("[data-error-dismiss]");
    var thumb = document.querySelector("[data-result-thumb]");
    var title = document.querySelector("[data-result-title]");
    var formatsList = document.querySelector("[data-result-formats]");
    var store = new Store();
    var messages = new Messages(form);
    var tr = function (key) { return messages.t(key); };
    var idleLabel = submitLabel ? submitLabel.textContent : "";

    var controller = null;
    var busy = false;

    /* ------------------------------ UI states ------------------------------ */

    function setBusy(on) {
      busy = on;
      if (submitBtn) {
        submitBtn.disabled = on;
        submitBtn.setAttribute("aria-busy", on ? "true" : "false");
      }
      if (submitLabel) submitLabel.textContent = on ? tr("hero.analyzing") : idleLabel;
      if (submitIcon) submitIcon.classList.toggle("animate-pulse", on);
      show(states.loading, on);
      if (on) {
        show(states.error, false);
        show(states.result, false);
      }
    }

    function setError(message, hint) {
      show(states.loading, false);
      show(states.result, false);
      if (errorMsg) errorMsg.textContent = message || tr("error.generic") || "Failed.";
      if (errorHint) {
        errorHint.textContent = hint || "";
        show(errorHint, Boolean(hint));
      }
      show(states.error, true);
      if (errorMsg) errorMsg.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }

    /* ----------------------------- result card ----------------------------- */

    function formatRow(fmt) {
      var li = el("li");
      var row = el("div", "flex flex-col gap-2 rounded-xl border border-slate-200 bg-white p-2.5 transition hover:border-indigo-300 hover:shadow-sm sm:flex-row sm:items-center dark:border-white/10 dark:bg-white/[.03] dark:hover:border-indigo-400/40");

      // Left: quality label + metadata chips.
      var left = el("div", "min-w-0 flex-1");
      var line = el("div", "flex flex-wrap items-center gap-1.5");
      line.appendChild(el("span", "text-sm font-bold", fmt.label));
      if (fmt.is_default) {
        line.appendChild(el("span", "rounded-full bg-indigo-50 px-2 py-0.5 text-[.625rem] font-bold uppercase tracking-wide text-indigo-700 ring-1 ring-indigo-200 dark:bg-indigo-500/15 dark:text-indigo-300 dark:ring-indigo-500/30", "★"));
      }
      left.appendChild(line);

      var meta = el("div", "mt-1 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[.6875rem] text-slate-500 dark:text-slate-400");
      function chip(text, cls) {
        if (!text) return;
        meta.appendChild(el("span", (cls || "") + " font-medium", text));
      }
      chip(fmt.filesize_text + (fmt.approx_note === "estimated" ? " ≈" : ""));
      if (fmt.bitrate_kbps) chip(fmt.bitrate_kbps + " kbps");
      if (fmt.fps && fmt.fps >= 50) chip(Math.round(fmt.fps) + " fps");
      if (fmt.height && fmt.kind !== "audio") chip(tr("results.quality") + " " + fmt.height + "p");
      if (fmt.quality_note) chip(fmt.quality_note, "opacity-80");
      if (fmt.kind === "video_only") chip(tr("results.video_only") || "video only", "text-amber-600 dark:text-amber-400");
      else if (fmt.kind === "video" && fmt.has_merged_audio) chip(tr("results.merged") || "merged", "text-emerald-600 dark:text-emerald-400");
      else if (fmt.kind === "audio") chip(tr("results.audio") || "audio", "text-fuchsia-600 dark:text-fuchsia-400");
      if (!fmt.streamable_in_browser) {
        chip(tr("results.not_streamable") || "HLS/DASH — copy the link into a player", "text-rose-600 dark:text-rose-400");
      }
      left.appendChild(meta);
      row.appendChild(left);

      // Right: actions. `<a href download>` for a real file, plus copy-to-clipboard.
      var actions = el("div", "flex shrink-0 items-center gap-1.5");
      var label = (fmt.kind === "audio" ? tr("results.download") + " ♪" : tr("results.download")) || "Download";
      if (fmt.streamable_in_browser) {
        var a = el("a", "ss-btn min-w-[7.5rem] bg-slate-900 py-2 text-xs text-white hover:brightness-125 dark:bg-white dark:text-slate-900" + (fmt.is_default ? " ring-2 ring-indigo-500/60" : ""), label);
        a.href = fmt.url;
        a.rel = "noopener noreferrer";
        a.target = "_self";
        a.setAttribute("download", "");
        actions.appendChild(a);
      } else {
        var disabled = el("span", "ss-btn min-w-[7.5rem] cursor-not-allowed bg-slate-100 py-2 text-xs text-slate-400 dark:bg-white/5", tr("results.copy") || "Copy link");
        actions.appendChild(disabled);
      }

      var copy = el("button", "ss-btn-ghost ss-btn rounded-lg px-2.5 py-2 text-xs", "");
      copy.type = "button";
      copy.appendChild(el("span", "font-semibold", tr("results.copy") || "Copy"));
      copy.addEventListener("click", function () {
        copyText(fmt.url).then(function (ok) {
          var span = copy.firstChild;
          if (!span) return;
          span.textContent = ok ? (tr("results.copied") || "Copied") : (tr("error.generic") || "Failed");
          window.setTimeout(function () {
            span.textContent = tr("results.copy") || "Copy";
          }, 1800);
        });
      });
      actions.appendChild(copy);

      row.appendChild(actions);
      li.appendChild(row);
      return li;
    }

    function renderResult(data) {
      if (title) title.textContent = data.title || "";
      if (formatsList) {
        while (formatsList.firstChild) formatsList.removeChild(formatsList.firstChild);
        var list = (data.formats || []).slice(0, 8);
        if (!list.length) {
          formatsList.appendChild(el("li", "rounded-xl bg-slate-50 p-4 text-sm text-slate-500 dark:bg-white/5", tr("results.empty") || "No formats."));
        }
        list.forEach(function (fmt) {
          formatsList.appendChild(formatRow(fmt));
        });
      }
      if (thumb) {
        if (data.thumbnail && /^https?:/.test(data.thumbnail)) {
          thumb.src = data.thumbnail;
          thumb.alt = data.title || "";
          thumb.parentNode.removeAttribute("hidden");
        } else {
          thumb.removeAttribute("src");
          thumb.alt = "";
          thumb.parentNode.setAttribute("hidden", "");
        }
      }
      setField("duration", data.duration_text);
      setField("uploader", data.uploader);
      setField("platform", data.platform);
      var elapsed = document.querySelector("[data-result-elapsed]");
      if (elapsed) {
        var text = (tr("results.elapsed") || "").replace("{ms}", String(data.resolve_ms || 0));
        elapsed.textContent = text;
        show(elapsed, Boolean(text.trim()));
      }
      show(states.result, true);
      show(states.error, false);
      if (data.cached && elapsed) elapsed.classList.add("opacity-70");
    }

    function setField(name, value) {
      var node = document.querySelector('[data-result-field="' + name + '"]');
      var row = document.querySelector('[data-result-row="' + name + '"]');
      if (!node) return;
      node.textContent = value || "";
      if (row) show(row, Boolean(value));
    }

    function copyText(text) {
      if (navigator.clipboard && window.isSecureContext) {
        return navigator.clipboard.writeText(text).then(
          function () { return true; },
          function () { return legacyCopy(text); }
        );
      }
      return Promise.resolve(legacyCopy(text));
    }

    function legacyCopy(text) {
      try {
        var ta = el("textarea", "sr-only fixed top-0 start-0 opacity-0");
        ta.value = text;
        ta.setAttribute("readonly", "");
        document.body.appendChild(ta);
        ta.select();
        var ok = document.execCommand("copy");
        document.body.removeChild(ta);
        return ok;
      } catch (e) {
        return false;
      }
    }

    /* ------------------------------ the fetch ------------------------------ */

    /**
     * Be forgiving about what people paste, without inventing anything:
     *   "  youtu.be/abc "  -> "https://youtu.be/abc"
     *   "Watch this https://x.com/a/b" -> "https://x.com/a/b"
     * Anything that still has no scheme after this is rejected server-side.
     */
    function normalize(raw) {
      var url = String(raw || "").trim();
      if (!url) return "";
      var found = url.match(/https?:\/\/\S+/i);
      if (found) return found[0];
      if (/^[a-z0-9.-]+\.[a-z]{2,}(\/|$|\?)/i.test(url)) return "https://" + url;
      return url;
    }

    function extract(url) {
      var payload = { url: url, platform: form.dataset.platform || null };
      var locale = form.dataset.locale || null;
      if (locale) payload.locale = locale;

      if (controller) controller.abort();
      controller = new AbortController();
      setBusy(true);

      var started = performance.now();
      window
        .fetch(ENDPOINT, {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify(payload),
          signal: controller.signal,
          credentials: "omit",
          mode: "same-origin",
        })
        .then(function (response) {
          return response
            .json()
            .catch(function () { return { error: { code: "http_" + response.status, message: "" } }; })
            .then(function (body) { return { status: response.status, body: body }; });
        })
        .then(function (outcome) {
          setBusy(false);
          var body = outcome.body || {};
          if (outcome.status === 429 && body.error && body.error.retry_after) {
            countdown(body.error.retry_after);
          }
          if (outcome.status >= 200 && outcome.status < 300 && body.ok) {
            if (!body.resolve_ms) body.resolve_ms = Math.round(performance.now() - started);
            store.write(url, body);
            renderResult(body);
          } else {
            setError(
              (body.error && body.error.message) || tr("error.generic"),
              body.error && body.error.hint
            );
          }
        })
        .catch(function (err) {
          setBusy(false);
          if (err && err.name === "AbortError") return;
          setError(tr("error.network") || "Network error.");
        });
    }

    var timer = null;
    function countdown(seconds) {
      window.clearInterval(timer);
      var left = Math.max(1, parseInt(seconds, 10) || 1);
      var base = tr("error.rate") || "Too many requests.";
      var node = errorMsg;
      if (!node) return;
      timer = window.setInterval(function () {
        left -= 1;
        if (left <= 0) {
          window.clearInterval(timer);
          node.textContent = base;
          return;
        }
        node.textContent = base + " (" + left + "s)";
      }, 1000);
    }

    /* ------------------------------- wiring -------------------------------- */

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var url = normalize(input ? input.value : "");
      if (!url) {
        show(states.loading, false);
        show(states.result, false);
        setError(tr("error.empty") || "Paste a link first.");
        if (input) input.focus();
        return;
      }
      if (input) input.value = url;
      var cached = store.read()[url];
      if (cached && cached.data) {
        var hit = JSON.parse(JSON.stringify(cached.data));
        hit.cached = true;
        hit.resolve_ms = 0;
        renderResult(hit);
        return;
      }
      extract(url);
    });

    if (pasteBtn) {
      pasteBtn.addEventListener("click", function () {
        if (!navigator.clipboard || !window.isSecureContext) {
          if (input) input.focus();
          return;
        }
        navigator.clipboard
          .readText()
          .then(function (text) {
            if (!input) return;
            input.value = normalize(text);
            input.focus();
            form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit", { cancelable: true }));
          })
          .catch(function () {
            if (input) input.focus();
          });
      });
    }

    if (clearBtn) {
      clearBtn.addEventListener("click", function () {
        if (!input) return;
        input.value = "";
        show(states.error, false);
        show(states.result, false);
        input.focus();
      });
    }

    if (errorDismiss) {
      errorDismiss.addEventListener("click", function () {
        show(states.error, false);
        if (input) input.focus();
      });
    }

    // Paste anywhere on the page (a downloader user's reflex) — but only when the
    // focus is not already inside a text field, so we never hijack a selection.
    document.addEventListener("paste", function (event) {
      var active = document.activeElement;
      if (active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName)) return;
      var text = event.clipboardData ? event.clipboardData.getData("text") : "";
      if (!text || !/^https?:\/\//i.test(text.trim())) return;
      if (input) input.value = normalize(text);
      window.setTimeout(function () {
        form.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
      }, 0);
    });

    // #ss-url deep links (used by the closing CTA) focus the field directly.
    if (window.location.hash === "#ss-url" && input) {
      input.focus();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", Boot);
  } else {
    Boot();
  }
})();
