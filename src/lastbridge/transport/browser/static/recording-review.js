/* Shared recording review; all mutations remain in the feature's own handler. */
(() => {
  "use strict";
  const PAGE_SIZE = 25;
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const button = (text, action, className = "review-button") => {
    const element = node("button", text, className);
    element.type = "button";
    element.addEventListener("click", action);
    return element;
  };
  const length = (seconds) => Number.isFinite(Number(seconds)) && seconds > 0
    ? `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`
    : "Unknown duration";
  const edition = (explicit) => explicit === true ? "Explicit"
    : explicit === false ? "Clean" : "Unknown explicitness";

  class Review {
    constructor(root) {
      this.root = root;
      this.model = JSON.parse(root.querySelector(".review-data").textContent);
      this.rows = root.querySelector(".review-rows");
      this.sheet = root.querySelector(".review-sheet");
      this.detail = root.querySelector(".review-detail");
      this.feedback = root.querySelector(".review-feedback");
      this.tab = "review";
      this.page = 0;
      this.query = "";
      this.reason = "all";
      this.busy = false;
      this.selection = null;
      this.undoChoice = null;
      this.message = "";
      this.requestId = 0;
      this.mobile = window.matchMedia("(max-width: 850px)");
      this.mobile.addEventListener("change", () => {
        if (this.selection) this.showSheet();
      });
      this.sheet.addEventListener("cancel", (event) => {
        event.preventDefault();
        if (!this.busy) this.close();
      });
      root.querySelector(".review-search").addEventListener("input", (event) => {
        this.query = event.target.value.toLocaleLowerCase();
        this.page = 0;
        this.renderList();
      });
      root.querySelector(".review-filter").addEventListener("change", (event) => {
        this.reason = event.target.value;
        this.page = 0;
        this.renderList();
      });
      this.renderList();
      const first = this.model.entries.find(e => e.key === this.model.selected);
      if (first) {
        this.tab = first.status;
        this.renderList();
        this.select(first.key);
      }
      this.poll();
    }

    filtered() {
      return this.model.entries.filter(e =>
        (this.tab === "all" || e.status === this.tab) &&
        `${e.artist} ${e.title}`.toLocaleLowerCase().includes(this.query) &&
        (this.reason === "all" || (this.reason === "none"
          ? e.status === "review" && !(e.candidates || []).length
          : (e.candidates || []).length > 1 || /multiple|ambiguous/i.test(e.reason || "")))
      );
    }

    renderList() {
      const scroll = this.rows.scrollTop;
      const counts = {review: 0, resolved: 0, skipped: 0};
      for (const entry of this.model.entries) counts[entry.status]++;
      this.root.querySelector(".review-summary").textContent =
        `${counts.review} need review · ${counts.resolved} ${this.model.kind === "playlist" ? "included" : "resolved"} · ${counts.skipped} skipped`;
      const tabs = this.root.querySelector(".review-tabs");
      tabs.replaceChildren();
      for (const [key, label] of [["review", "Needs review"], ["resolved", "Resolved"],
        ["skipped", "Skipped"], ["all", "All"]]) {
        const tab = button(`${label} (${key === "all" ? this.model.entries.length : counts[key]})`, () => {
          this.tab = key;
          this.page = 0;
          this.renderList();
        });
        tab.setAttribute("aria-pressed", String(this.tab === key));
        tabs.append(tab);
      }
      const entries = this.filtered();
      this.page = Math.max(0, Math.min(this.page, Math.ceil(entries.length / PAGE_SIZE) - 1));
      this.rows.replaceChildren();
      for (const entry of entries.slice(this.page * PAGE_SIZE, (this.page + 1) * PAGE_SIZE)) {
        const row = node("li");
        const open = button("", () => this.select(entry.key), "review-row");
        open.dataset.status = entry.status;
        open.setAttribute("aria-current", String(this.selection === entry.key));
        const name = node("span", undefined, "review-track");
        name.append(node("strong", entry.title), node("small", entry.artist));
        open.append(name, node("span", `${entry.status === "review" ? "!" : entry.status === "skipped" ? "−" : "✓"} ${entry.label}`, "review-status"));
        if (entry.reason) name.append(node("small", entry.reason));
        row.append(open);
        this.rows.append(row);
      }
      if (!entries.length) this.rows.append(node("li", "No tracks in this view.", "review-empty"));
      this.rows.scrollTop = scroll;
      const pages = this.root.querySelector(".review-pagination");
      const previous = button("Previous", () => { this.page--; this.renderList(); this.rows.scrollTop = 0; });
      previous.disabled = this.page === 0;
      const next = button("Next", () => { this.page++; this.renderList(); this.rows.scrollTop = 0; });
      next.disabled = (this.page + 1) * PAGE_SIZE >= entries.length;
      pages.replaceChildren(previous, node("span", `${entries.length ? this.page * PAGE_SIZE + 1 : 0}–${Math.min(entries.length, (this.page + 1) * PAGE_SIZE)} of ${entries.length}`), next);
      this.root.querySelectorAll("button, input, select").forEach(e => { e.disabled = this.busy || e.disabled; });
      const count = document.querySelector("[data-playlist-count]");
      if (count) count.textContent = `${counts.resolved} playable / ${this.model.entries.length} entries${this.model.scope ? ` · ${this.model.scope}` : ""}`;
    }

    async request(path, values) {
      const options = {headers: {Accept: "application/json"}, credentials: "same-origin"};
      if (values !== undefined) {
        options.method = "POST";
        options.body = new URLSearchParams({csrf: this.root.dataset.csrf, ...values});
      }
      const response = await fetch(path, options);
      const data = await response.json().catch(() => ({}));
      if (!response.ok || response.redirected) throw new Error(data.detail || "Could not save; try again.");
      return data;
    }

    showSheet() {
      const focused = document.activeElement;
      if (this.sheet.open) this.sheet.close();
      if (this.mobile.matches) this.sheet.showModal();
      else this.sheet.show();
      if (focused && this.sheet.contains(focused)) focused.focus({preventScroll: true});
      else {
        const heading = this.detail.querySelector("h2");
        if (heading) { heading.tabIndex = -1; heading.focus({preventScroll: true}); }
      }
    }

    close() {
      const key = this.selection;
      this.selection = null;
      this.requestId++;
      this.sheet.close();
      this.renderList();
      const index = this.filtered().findIndex(e => e.key === key);
      const target = index >= 0
        ? this.rows.children[index % PAGE_SIZE]?.querySelector("button")
        : this.root.querySelector('.review-tabs button[aria-pressed="true"]');
      target?.focus({preventScroll: true});
    }

    async select(key) {
      if (this.busy) return;
      const requestId = ++this.requestId;
      this.selection = key;
      const position = this.filtered().findIndex(e => e.key === key);
      if (position >= 0 && Math.floor(position / PAGE_SIZE) !== this.page) {
        this.page = Math.floor(position / PAGE_SIZE);
        this.rows.scrollTop = 0;
      }
      this.renderList();
      let entry = this.model.entries.find(e => e.key === key);
      this.renderDetail(entry);
      this.showSheet();
      if (entry.kind !== "playlist" && !entry.loaded) {
        try {
          const data = await this.request(entry.path);
          if (requestId !== this.requestId) return;
          this.update(data);
          entry = data.entry;
          this.renderDetail(entry);
        } catch (error) { this.error(error); return; }
      }
      if (entry.kind !== "playlist" && !entry.searched && !entry.skipped && entry.status === "review") {
        await this.save(entry, "search", {query: ""});
      }
    }

    update(data, invalidate = false) {
      if (data.model) {
        this.model = data.model;
        this.updateHeader();
      }
      if (data.entry) this.model.entries = this.model.entries.map(e =>
        e.key === data.entry.key ? data.entry : invalidate
          ? {...e, loaded: false, searched: false, candidates: [], token: ""} : e);

      this.renderList();
    }

    updateHeader() {
      if (!this.model.header) return;
      const header = document.querySelector("[data-import-header]");
      if (header) {
        const fragment = new DOMParser().parseFromString(this.model.header, "text/html");
        header.replaceChildren(...fragment.body.childNodes);
      }
    }

    notice(message, isError = false) {
      this.message = message;
      for (const feedback of this.root.querySelectorAll(".review-feedback")) {
        feedback.setAttribute("role", isError ? "alert" : "status");
        feedback.replaceChildren(node("span", message, isError ? "review-error" : ""));
        if (this.undoChoice) {
          const {entry, token} = this.undoChoice;
          feedback.append(button("Undo", () => this.save(entry, "undo", {undo: token})));
        }
      }
    }

    error(error) { this.notice(error.message, true); }

    async save(entry, action, values = {}) {
      if (this.busy) return;
      this.busy = true;
      this.root.setAttribute("aria-busy", "true");
      const previous = this.filtered();
      const index = previous.findIndex(e => e.key === entry.key);
      const nextKey = (previous.slice(index + 1).find(e => e.key !== entry.key && e.status === "review") ||
        previous.slice(0, index).reverse().find(e => e.key !== entry.key && e.status === "review"))?.key;
      this.root.querySelectorAll("button, input, select").forEach(e => { e.disabled = true; });
      try {
        const data = await this.request(`${entry.path}/${action}`, {
          source_key: entry.key, ...values,
        });
        this.update(data, ["choose", "clear", "undo"].includes(action));
        if (action !== "search") {
          this.undoChoice = action === "undo" ? null :
            data.undo ? {entry, token: data.undo} : null;
          this.notice(action === "undo" ? "Choice undone." : "Saved.");
        }
        this.busy = false;
        if (["choose", "skip"].includes(action) && this.model.kind !== "song") {
          const next = this.model.entries.find(e => e.key === nextKey && e.status === "review");
          if (next) await this.select(next.key);
          else this.close();
        } else {
          const current = this.model.entries.find(e => e.key === entry.key);
          this.selection = entry.key;
          this.renderDetail(current);
          this.showSheet();
        }
      } catch (error) {
        this.error(error);
      } finally {
        this.busy = false;
        this.root.removeAttribute("aria-busy");
        this.renderList();
        // Candidate controls are not part of the queue's pagination state.
        this.detail.querySelectorAll("button, input").forEach(e => { e.disabled = false; });
        this.root.querySelectorAll(".review-toolbar button, .review-toolbar input, .review-toolbar select, .review-feedback button").forEach(e => { e.disabled = false; });
      }
    }

    renderDetail(entry) {
      this.detail.replaceChildren();
      const feedback = node("div", undefined, "review-feedback");
      feedback.setAttribute("aria-live", "polite");
      this.detail.append(feedback);
      if (this.message) this.notice(this.message);
      const heading = node("div", undefined, "review-detail-heading");
      heading.append(node("h3", "Original track"), button("Close", () => this.close()));
      this.detail.append(heading, node("h2", entry.title), node("p", entry.artist));
      this.detail.append(node("p", `${length(entry.duration)} · ${edition(entry.explicit)}`, "muted"));
      if (entry.reason) this.detail.append(node("p", entry.reason, "review-warning"));
      if (entry.current) this.detail.append(node("p", `Current recording: ${entry.current.artist} — ${entry.current.title} · ${entry.current.album || ""}`, "notice"));
      if (!this.model.editable) {
        this.detail.append(node("p", "Only the administrator can change this shared chart."));
        return;
      }
      const actions = node("div", undefined, "review-entry-actions");
      if (entry.skipped) {
        this.detail.append(node("p", entry.kind === "playlist" ? "Excluded from this playlist." : "Kept on Last.fm; excluded from local Favorites."));
        actions.append(button("Restore track", () => this.save(entry, "restore")));
      } else if (entry.kind === "playlist" || (entry.kind === "love" && !entry.song_id)) {
        actions.append(button("Skip track", () => this.save(entry, "skip")));
      } else if (entry.current) {
        actions.append(button("Keep current recording", () => {
          entry.status = "resolved";
          entry.label = "Kept current recording";
          this.close();
        }), button("Review later", () => this.close()));
      }
      if (entry.can_clear) actions.append(button("Use original recording", () => this.save(entry, "clear")));
      this.detail.append(actions);
      if (entry.skipped) return;
      if (entry.kind !== "playlist") {
        const form = node("form", undefined, "review-search-form");
        const label = node("label", "Search recordings");
        const input = node("input");
        input.name = "query";
        input.maxLength = 256;
        input.value = `${entry.artist} ${entry.title}`.slice(0, 256);
        label.append(input);
        const search = node("button", "Search");
        search.type = "submit";
        form.append(label, search);
        form.addEventListener("submit", event => {
          event.preventDefault();
          this.save(entry, "search", {query: input.value});
        });
        this.detail.append(form);
      }
      this.detail.append(node("h3", "Recordings"));
      for (const candidate of entry.candidates || []) {
        const card = node("article", undefined, "review-candidate");
        card.append(node("strong", `${candidate.artist} — ${candidate.title}`),
          node("p", `${candidate.album || "Unknown album"} · ${length(candidate.duration)} · ${edition(candidate.explicit)}`));
        if (candidate.source) {
          const link = node("a", "Listen on YouTube Music");
          link.href = `https://music.youtube.com/watch?v=${encodeURIComponent(candidate.source)}`;
          link.target = "_blank";
          link.rel = "noreferrer";
          card.append(link);
        }
        if (candidate.warnings?.length) card.append(node("p", candidate.warnings.join(" · "), "review-warning"));
        const choose = () => this.save(entry, "choose", {
          song_id: candidate.song_id, token: entry.token || "", acknowledge: "yes",
        });
        card.append(button("Use recording", () => {
          if (!candidate.mismatch) { choose(); return; }
          const confirmation = node("div", undefined, "review-confirmation");
          confirmation.append(node("p", "This recording differs from the original track. Check the artist, version, duration and explicitness above."),
            button("Use anyway", choose), button("Cancel", () => confirmation.remove()));
          card.querySelector(".review-confirmation")?.remove();
          card.append(confirmation);
          confirmation.querySelector("button").focus({preventScroll: true});
        }));
        this.detail.append(card);
      }
      if (!(entry.candidates || []).length) this.detail.append(node("p", entry.loaded === false
        ? "Loading recording choices…" : "No recording matches. You can skip this track or try another search."));
    }

    async poll() {
      if (!this.model.running) return;
      setTimeout(async () => {
        try {
          if (!this.busy && !this.selection) {
            this.model = await this.request(this.model.path);
            this.renderList();
            this.updateHeader();
          }
        } catch (error) { this.error(error); }
        this.poll();
      }, 5000);
    }
  }
  document.querySelectorAll("[data-recording-review]").forEach(root => new Review(root));
})();
