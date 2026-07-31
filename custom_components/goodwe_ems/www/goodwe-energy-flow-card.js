/**
 * GoodWe Energy Flow Card
 *
 * Diagramă de flux energetic cu preț PZU orar și câștig lunar de prosumator.
 *
 * Cele patru noduri sunt legate de un hub central, nu între ele: la un invertor
 * hibrid fiecare kilowatt trece fizic prin invertor, iar o săgeată directă
 * PV -> casă ar sugera un traseu care nu există.
 *
 * Punctele care aleargă pe trasee sunt tot stroke-dashoffset, nu animateMotion:
 * o liniuță de lungime aproape zero cu `stroke-linecap: round` se desenează ca
 * un cerc, deci se obține exact aspectul de bilă în mișcare, dar animația se
 * compozitează pe GPU, iar inversarea sensului import/export cere doar
 * animation-direction, nu redesenarea traseului.
 */

const CARD_VERSION = "1.3.0";

const DOMAIN = "goodwe_ems";

// Câmpul cardului -> `translation_key`-ul entității din integrare.
//
// `entity_id`-urile nu pot fi ghicite: Home Assistant le compune din numele
// tradus al entității în momentul creării, deci pe o instanță în română ies
// `sensor.goodwe_ems_putere_pv`, nu `sensor.goodwe_ems_pv_power`. Cheia de
// traducere e însă aceeași în orice limbă, iar registrul de entități o expune.
//
// `monthly_profit` lipsește intenționat: e un senzor șablon al utilizatorului,
// integrarea nu are de unde să-l producă (rețeta e în README).
const DISCOVERY = {
  pv_power: "pv_power",
  load_power: "load_power",
  grid_power: "ac_active_power",
  battery_power: "battery_power",
  battery_soc: "battery_soc",
  pzu_price: "pzu_price",
};

// Romb, ca la cardul „Energy distribution" din Home Assistant: soare sus,
// rețea stânga, casă dreapta, baterie jos. Bateria stă jos și nu lateral
// pentru că e singurul nod bidirecțional al schemei — pe verticală, sub hub,
// săgeata în sus / în jos se citește fără să te uiți la etichetă.
const HUB = { x: 200, y: 200, r: 26 };
const NODE_R = 34;
// Distanța dintre inel și capătul traseului. Fără ea, punctele par să iasă
// din cerc, nu să curgă spre el.
const GAP = 6;

const NODES = {
  pv: { x: 200, y: 70, label: "Solar", labelAbove: true },
  grid: { x: 62, y: 200, label: "Rețea" },
  load: { x: 338, y: 200, label: "Consumatori" },
  battery: { x: 200, y: 330, label: "Baterie" },
};

// Fiecare nod are culoarea lui, ca inelul să spună despre ce e vorba înainte
// de a citi eticheta. Se pot rescrie din tema utilizatorului; rezerva a doua
// e variabila Home Assistant, unde există una cu înțelesul potrivit.
const COLORS = {
  pv: "var(--goodwe-pv-color, var(--energy-solar-color, #f5a623))",
  grid: "var(--goodwe-grid-color, var(--energy-grid-consumption-color, #5a9fd4))",
  load: "var(--goodwe-load-color, #3fc4c4)",
  battery: "var(--goodwe-battery-color, #e5559f)",
  hub: "var(--goodwe-hub-color, var(--secondary-text-color, #9e9e9e))",
};

const DEFAULTS = {
  min_flow_watts: 30,
  decimals: 1,
  invert_grid: false,
  // Convenția de semn a registrului 35182 nu e documentată în harta ARM 745.
  // Verific-o o dată, cu bateria vizibil în încărcare, și comută flagul dacă
  // săgeata arată invers. Pentru senzorul `pbattery1` al integrării GoodWe
  // oficiale (pozitiv la descărcare) valoarea corectă este `true`.
  invert_battery: false,
  hub_label: "Invertor",
  // Cardul din Home Assistant nu desenează invertorul, fiindcă lucrează cu
  // energie contorizată, unde nu contează prin ce trece. Aici trece: pune
  // `show_inverter: false` și în centru rămâne doar nodul de joncțiune.
  show_inverter: true,
};

// Iconițe desenate de la zero, toate cu aceeași grosime de linie, ca să pară o
// familie coerentă. Fără dependință de MDI.
const ICONS = {
  pv: `
    <path d="M-16 8 L-11 -8 L11 -8 L16 8 Z" />
    <path d="M-13.5 0 L13.5 0" />
    <path d="M-3 -8 L-5.5 8" />
    <path d="M3 -8 L5.5 8" />
    <path d="M0 -14 L0 -18" />
    <path d="M-10 -12 L-12 -15.5" />
    <path d="M10 -12 L12 -15.5" />`,
  grid: `
    <path d="M-9 12 L-4 -10 L4 -10 L9 12" />
    <path d="M-7 3 L7 3" />
    <path d="M-5.5 -3.5 L5.5 -3.5" />
    <path d="M-6.2 3 L6.2 -3.5 M6.2 3 L-6.2 -3.5" />
    <path d="M-11 -10 L11 -10" />
    <path d="M-4 -10 L0 -14 L4 -10" />`,
  battery: `
    <rect x="-9" y="-12" width="18" height="24" rx="2.5" />
    <path d="M-3.5 -14.5 L3.5 -14.5" />`,
  load: `
    <path d="M-14 0 L0 -12 L14 0" />
    <path d="M-10.5 -2 L-10.5 12 L10.5 12 L10.5 -2" />
    <path d="M-3.5 12 L-3.5 3 L3.5 3 L3.5 12" />`,
  hub: `
    <rect x="-13" y="-16" width="26" height="32" rx="3" />
    <path d="M-7 -6 C-4 -12, -1 0, 2 -6 S 6 -12, 8 -6" />
    <path d="M-7 7 L8 7" />`,
};

class GoodweEnergyFlowCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._rendered = false;
  }

  static getStubConfig() {
    // Fără entități: cardul le găsește singur din registru, deci galeria arată
    // o previzualizare corectă indiferent de limba instanței.
    return { type: "custom:goodwe-energy-flow-card" };
  }

  setConfig(config) {
    if (!config) throw new Error("Configurație lipsă");
    this._config = { ...DEFAULTS, ...config };
    this._rendered = false;
    // Configurația poate fi rescrisă din editor: o entitate ștearsă acolo
    // trebuie recăutată, nu preluată din descoperirea anterioară.
    this._discovered = false;
    this._discovering = false;
    if (this.shadowRoot) this.shadowRoot.innerHTML = "";
  }

  getCardSize() {
    return 6;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    if (!this._rendered) {
      this._build();
      this._rendered = true;
    }
    if (!this._discovered && !this._discovering && this._unresolved().length) {
      this._discover();
    }
    this._update();
  }

  // ------------------------------------------------------- descoperire

  /** Câmpurile pe care utilizatorul nu le-a scris explicit în YAML. */
  _unresolved() {
    return Object.keys(DISCOVERY).filter((key) => !this._config[key]);
  }

  /**
   * Completează entitățile lipsă din registrul de entități.
   *
   * Ce e scris în YAML rămâne neatins — descoperirea umple doar golurile, ca
   * un card configurat manual să nu-și schimbe comportamentul.
   *
   * `config/entity_registry/list` nu cere drepturi de administrator, deci
   * merge și pentru un utilizator obișnuit care doar se uită la tablou.
   */
  async _discover() {
    this._discovering = true;
    try {
      const entries = await this._hass.callWS({
        type: "config/entity_registry/list",
      });
      const mine = entries.filter(
        (e) => e.platform === DOMAIN && !e.disabled_by
      );

      // Cu două invertoare, entitățile lor s-ar amesteca într-o singură
      // diagramă. Se grupează pe intrarea de configurare și se ia un singur
      // grup: cel cerut explicit, altfel cel mai complet (la egalitate,
      // primul în ordine alfabetică, ca alegerea să nu depindă de ordinea în
      // care a răspuns registrul).
      const groups = new Map();
      for (const entry of mine) {
        const id = entry.config_entry_id || "";
        if (!groups.has(id)) groups.set(id, []);
        groups.get(id).push(entry);
      }
      const wanted = this._config.config_entry_id;
      const group =
        (wanted && groups.get(wanted)) ||
        [...groups.entries()].sort(
          (a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0])
        )[0]?.[1] ||
        [];

      const found = {};
      for (const key of this._unresolved()) {
        const tkey = DISCOVERY[key];
        // `unique_id` e rezerva pentru cazul în care registrul nu a reținut
        // cheia de traducere; integrarea îl compune ca `{entry_id}_{cheie}`.
        const hit =
          group.find((e) => e.translation_key === tkey) ||
          group.find((e) => e.unique_id && e.unique_id.endsWith(`_${tkey}`));
        if (hit) found[key] = hit.entity_id;
      }

      this._config = { ...this._config, ...found };
      this._discovered = true;
      if (Object.keys(found).length) {
        console.info(
          `[goodwe-energy-flow-card] entități descoperite: ${JSON.stringify(found)}`
        );
      }
    } catch (err) {
      // Un card fără diagramă e destul de vizibil; jurnalul explică de ce.
      console.warn("[goodwe-energy-flow-card] descoperirea a eșuat:", err);
    } finally {
      this._discovering = false;
      if (this._rendered && this._hass) this._update();
    }
  }

  // ---------------------------------------------------------------- schelet

  _build() {
    const c = this._config;
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { padding: 16px; }
        .header {
          display: flex; justify-content: space-between; align-items: baseline;
          margin-bottom: 4px;
        }
        .title { font-size: 1.05rem; font-weight: 500; }
        .price {
          font-size: 0.95rem; font-variant-numeric: tabular-nums;
          color: var(--primary-color);
        }
        .price .unit { font-size: 0.75rem; opacity: 0.7; margin-left: 2px; }
        svg { width: 100%; height: auto; display: block; }
        .rail { stroke: var(--divider-color, #d0d0d0); stroke-width: 1.6; fill: none; opacity: 0.7; }
        /* Liniuța de 0.1 cu capăt rotund se randează ca un punct de diametrul
           grosimii; restul perioadei e spațiu. Offsetul din @keyframes trebuie
           să fie exact perioada (0.1 + 25.9), altfel bucla sare vizibil. */
        .flow {
          stroke-width: 7; fill: none; stroke-linecap: round;
          stroke-dasharray: 0.1 25.9;
          animation: dash linear infinite;
        }
        @keyframes dash { to { stroke-dashoffset: -26; } }
        .flow.hidden { display: none; }
        .icon { fill: none; stroke-width: 2.8; stroke-linecap: round; stroke-linejoin: round; }
        .node { cursor: pointer; }
        .node:hover .ring { stroke-width: 5.5; }
        .ring {
          fill: var(--ha-card-background, var(--card-background-color, #fff));
          stroke-width: 4;
        }
        .value {
          font-size: 14px; font-weight: 500; text-anchor: middle;
          font-variant-numeric: tabular-nums;
        }
        .label {
          font-size: 11px; text-anchor: middle;
          fill: var(--secondary-text-color);
        }
        .soc { font-size: 10.5px; text-anchor: middle; fill: var(--secondary-text-color); }
        /* Eticheta hubului stă în cadranul liber dintre traseul spre soare și
           cel spre rețea. Conturul în culoarea cardului o ține lizibilă chiar
           dacă o temă cu alte proporții o împinge peste o linie. */
        .hub-label {
          font-size: 10.5px; text-anchor: end;
          fill: var(--secondary-text-color);
          stroke: var(--ha-card-background, var(--card-background-color, #fff));
          stroke-width: 3px; paint-order: stroke fill;
        }
        .junction { fill: var(--divider-color, #d0d0d0); }
        .footer {
          display: flex; justify-content: space-between; align-items: center;
          margin-top: 6px; font-size: 0.85rem; color: var(--secondary-text-color);
        }
        .profit { font-variant-numeric: tabular-nums; color: var(--primary-text-color); }
        .warn {
          margin-top: 8px; padding: 6px 8px; border-radius: 6px;
          background: var(--warning-color, #ffa726); color: #202020;
          font-size: 0.8rem;
        }
      </style>
      <ha-card>
        <div class="header">
          <span class="title">${this._escape(c.title || "Flux energetic")}</span>
          <span class="price" id="price"></span>
        </div>
        <svg viewBox="0 0 400 410" xmlns="http://www.w3.org/2000/svg">
          ${this._rails()}
          ${this._flows()}
          ${this._node("pv")}
          ${this._node("grid")}
          ${this._node("load")}
          ${this._node("battery")}
          ${this._hub()}
        </svg>
        ${
          c.monthly_profit
            ? `<div class="footer">
          <span>Câștig luna curentă</span>
          <span class="profit" id="profit">—</span>
        </div>`
            : ""
        }
        <div class="warn" id="warn" style="display:none"></div>
      </ha-card>
    `;

    for (const key of Object.keys(NODES)) {
      const el = this.shadowRoot.getElementById(`node-${key}`);
      el.addEventListener("click", () => {
        const entity = this._entityFor(key);
        if (entity) this._openMoreInfo(entity);
      });
    }
  }

  _rails() {
    return Object.entries(NODES)
      .map(([key, n]) => `<path class="rail" d="${this._path(n)}" />`)
      .join("");
  }

  _flows() {
    return Object.entries(NODES)
      .map(
        ([key, n]) =>
          `<path class="flow hidden" id="flow-${key}" stroke="${COLORS[key]}"
             d="${this._path(n)}" />`
      )
      .join("");
  }

  /**
   * Segmentul dintre marginea inelului și marginea hubului.
   *
   * Traseul se oprește la inele, nu la centre, ca punctele să nu dispară sub
   * ele: inelul e opac, iar un punct înghițit la jumătatea drumului ar arăta
   * ca o animație care se blochează.
   */
  _path(node) {
    const dx = HUB.x - node.x;
    const dy = HUB.y - node.y;
    const len = Math.hypot(dx, dy) || 1;
    const ux = dx / len;
    const uy = dy / len;
    // Fără invertor desenat, traseele se întâlnesc în punctul de joncțiune.
    const hubR = this._config.show_inverter === false ? 0 : HUB.r + GAP;
    const x1 = node.x + ux * (NODE_R + GAP);
    const y1 = node.y + uy * (NODE_R + GAP);
    const x2 = HUB.x - ux * hubR;
    const y2 = HUB.y - uy * hubR;
    return `M ${x1.toFixed(1)} ${y1.toFixed(1)} L ${x2.toFixed(1)} ${y2.toFixed(1)}`;
  }

  /**
   * Un nod: inel colorat, iconiță în jumătatea de sus, valoarea sub ea — tot
   * în interiorul inelului — și eticheta afară. Valoarea preia culoarea
   * inelului, ca perechea cerc-cifră să se citească dintr-o privire.
   */
  _node(key) {
    const n = NODES[key];
    const color = COLORS[key];
    const labelY = n.labelAbove ? n.y - NODE_R - 12 : n.y + NODE_R + 18;
    return `
      <g class="node" id="node-${key}">
        <circle class="ring" cx="${n.x}" cy="${n.y}" r="${NODE_R}" stroke="${color}" />
        <g class="icon" transform="translate(${n.x},${n.y - 11}) scale(0.6)"
           stroke="${color}" id="icon-${key}">
          ${ICONS[key]}
        </g>
        <text class="value" x="${n.x}" y="${n.y + 17}" fill="${color}"
              id="value-${key}">—</text>
        <text class="label" x="${n.x}" y="${labelY}">${n.label}</text>
        ${
          key === "battery"
            ? `<text class="soc" x="${n.x}" y="${labelY + 15}" id="soc">—</text>`
            : ""
        }
      </g>`;
  }

  _hub() {
    if (this._config.show_inverter === false) {
      return `<circle class="junction" cx="${HUB.x}" cy="${HUB.y}" r="4" />`;
    }
    return `
      <g>
        <circle class="ring" cx="${HUB.x}" cy="${HUB.y}" r="${HUB.r}"
                stroke="${COLORS.hub}" />
        <g class="icon" transform="translate(${HUB.x},${HUB.y}) scale(0.62)"
           stroke="${COLORS.hub}">
          ${ICONS.hub}
        </g>
        <text class="hub-label" x="${HUB.x - HUB.r - 6}" y="${HUB.y - 22}">${this._escape(
          this._config.hub_label
        )}</text>
      </g>`;
  }

  // ---------------------------------------------------------------- date

  _update() {
    const c = this._config;
    const missing = [];

    const pv = this._watts(c.pv_power, missing);
    const load = this._watts(c.load_power, missing);
    let grid = this._watts(c.grid_power, missing);
    let battery = this._watts(c.battery_power, missing);

    if (c.invert_grid) grid = -grid;
    if (c.invert_battery) battery = -battery;

    // Convenție internă a cardului, după inversări:
    //   grid    > 0 = import din rețea
    //   battery > 0 = încărcare
    //
    // Traseele sunt desenate dinspre nod spre hub, deci `reverse` înseamnă
    // „dinspre invertor spre nod". Consumatorii primesc întotdeauna, deci
    // acolo e mereu reverse; bateria doar cât se încarcă.
    this._setFlow("pv", pv, false);
    this._setFlow("load", load, true);
    this._setFlow("grid", grid, grid < 0);
    this._setFlow("battery", battery, battery > 0);

    this._setValue("pv", pv);
    this._setValue("load", load);
    this._setValue("grid", grid);
    this._setValue("battery", battery);

    this._updateBatteryIcon(missing);
    this._updatePrice(missing);
    this._updateProfit(missing);

    const warn = this.shadowRoot.getElementById("warn");
    warn.textContent = this._warning(missing);
    warn.style.display = warn.textContent ? "" : "none";
  }

  /**
   * Textul de sub diagramă. Cât timp descoperirea e în curs nu se anunță
   * nimic: entitățile chiar lipsesc din configurație în acel moment, iar un
   * avertisment care apare și dispare singur ar trimite pe o pistă falsă.
   */
  _warning(missing) {
    if (this._discovering) return "";
    const unresolved = this._unresolved();
    if (unresolved.length === Object.keys(DISCOVERY).length) {
      return (
        "Nu am găsit entitățile GoodWe EMS. Verifică dacă integrarea e " +
        "configurată, sau scrie entitățile în YAML-ul cardului."
      );
    }
    const parts = [];
    // Descoperit parțial: pe o instalație completă nu se întâmplă, deci merită
    // spus care câmpuri au rămas fără entitate, nu doar desenate cu zero.
    if (unresolved.length) {
      parts.push(`Câmpuri fără entitate: ${unresolved.join(", ")}`);
    }
    if (missing.length) {
      parts.push(`Entități indisponibile: ${missing.join(", ")}`);
    }
    return parts.join(" · ");
  }

  _setFlow(key, watts, reverse) {
    const el = this.shadowRoot.getElementById(`flow-${key}`);
    const abs = Math.abs(watts);
    if (abs < this._config.min_flow_watts) {
      el.classList.add("hidden");
      return;
    }
    el.classList.remove("hidden");
    // Durata scalează invers cu puterea: 2,6 s în repaus, 0,5 s la 5 kW.
    const ratio = Math.min(abs / 5000, 1);
    el.style.animationDuration = `${(2.6 - 2.1 * ratio).toFixed(2)}s`;
    el.style.animationDirection = reverse ? "reverse" : "normal";
  }

  _setValue(key, watts) {
    this.shadowRoot.getElementById(`value-${key}`).textContent = this._power(watts);
  }

  _updateBatteryIcon(missing) {
    const soc = this._num(this._config.battery_soc, missing);
    const socEl = this.shadowRoot.getElementById("soc");
    const icon = this.shadowRoot.getElementById("icon-battery");
    if (socEl) socEl.textContent = soc === null ? "—" : `${Math.round(soc)} %`;
    if (!icon) return;

    // Nivelul din iconiță urmărește SOC-ul; sub 15 % trece pe roșu.
    const existing = icon.querySelector(".soc-fill");
    if (existing) existing.remove();
    if (soc === null) return;

    const height = Math.max(0, Math.min(1, soc / 100)) * 22;
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("class", "soc-fill");
    rect.setAttribute("x", "-8");
    rect.setAttribute("y", String(11 - height));
    rect.setAttribute("width", "16");
    rect.setAttribute("height", String(height));
    rect.setAttribute("rx", "1.5");
    rect.setAttribute("stroke", "none");
    rect.setAttribute(
      "fill",
      soc < 15 ? "var(--error-color, #db4437)" : COLORS.battery
    );
    rect.setAttribute("opacity", "0.85");
    icon.insertBefore(rect, icon.firstChild);
  }

  _updatePrice(missing) {
    const el = this.shadowRoot.getElementById("price");
    const price = this._num(this._config.pzu_price, missing);
    if (price === null) {
      el.textContent = "";
      return;
    }
    const unit = this._unit(this._config.pzu_price) || "lei/kWh";
    el.innerHTML = `${this._fmt(price, 3)}<span class="unit">${this._escape(unit)}</span>`;
  }

  _updateProfit(missing) {
    // Rândul e desenat doar când `monthly_profit` e configurat.
    const el = this.shadowRoot.getElementById("profit");
    if (!el) return;
    const profit = this._num(this._config.monthly_profit, missing);
    el.textContent = profit === null ? "—" : `${this._fmt(profit, 2)} lei`;
  }

  _entityFor(key) {
    const c = this._config;
    return {
      pv: c.pv_power,
      grid: c.grid_power,
      battery: c.battery_power,
      load: c.load_power,
    }[key];
  }

  // ---------------------------------------------------------------- utile

  _state(entityId) {
    return entityId && this._hass ? this._hass.states[entityId] : undefined;
  }

  _num(entityId, missing) {
    if (!entityId) return null;
    const s = this._state(entityId);
    if (!s || s.state === "unavailable" || s.state === "unknown") {
      if (missing) missing.push(entityId);
      return null;
    }
    const v = Number(s.state);
    return Number.isFinite(v) ? v : null;
  }

  _unit(entityId) {
    const s = this._state(entityId);
    return s ? s.attributes.unit_of_measurement : null;
  }

  /** Întoarce valoarea în wați, convertind automat din kW dacă e cazul. */
  _watts(entityId, missing) {
    const v = this._num(entityId, missing);
    if (v === null) return 0;
    const unit = (this._unit(entityId) || "").toLowerCase();
    return unit === "kw" ? v * 1000 : v;
  }

  _power(watts) {
    const abs = Math.abs(watts);
    if (abs < 1000) return `${Math.round(abs)} W`;
    return `${this._fmt(abs / 1000, this._config.decimals)} kW`;
  }

  _fmt(value, decimals) {
    return new Intl.NumberFormat("ro-RO", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    }).format(value);
  }

  _escape(str) {
    return String(str).replace(
      /[&<>"']/g,
      (ch) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])
    );
  }

  _openMoreInfo(entityId) {
    const ev = new Event("hass-more-info", { bubbles: true, composed: true });
    ev.detail = { entityId };
    this.dispatchEvent(ev);
  }
}

// Fișierul poate ajunge încărcat de două ori: o dată prin `extra_js_url` din
// integrare și o dată dacă a fost adăugat manual ca resursă Lovelace. Fără
// garda asta, al doilea `define` aruncă și cardul dispare complet — inclusiv
// instanța care se încărcase corect prima dată.
if (!customElements.get("goodwe-energy-flow-card")) {
  customElements.define("goodwe-energy-flow-card", GoodweEnergyFlowCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "goodwe-energy-flow-card")) {
window.customCards.push({
  type: "goodwe-energy-flow-card",
  name: "GoodWe Energy Flow Card",
  description: "Diagramă de flux energetic cu preț PZU orar și câștig lunar.",
  preview: true,
});
}

console.info(
  `%c GOODWE-ENERGY-FLOW-CARD %c v${CARD_VERSION} `,
  "color:#fff;background:#1D9E75;font-weight:500",
  "color:#1D9E75;background:transparent"
);
