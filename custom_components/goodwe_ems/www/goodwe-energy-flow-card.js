/**
 * GoodWe Energy Flow Card
 *
 * Diagramă de flux energetic cu preț PZU orar și câștig lunar de prosumator.
 *
 * Cele patru noduri sunt legate de un hub central, nu între ele: la un invertor
 * hibrid fiecare kilowatt trece fizic prin invertor, iar o săgeată directă
 * PV -> casă ar sugera un traseu care nu există.
 *
 * Animația e stroke-dashoffset pe un traseu suprapus peste o șină gri, nu
 * puncte cu animateMotion — se compozitează pe GPU, iar inversarea sensului
 * import/export cere doar animation-direction, nu redesenarea traseului.
 */

const CARD_VERSION = "1.1.0";

const HUB = { x: 200, y: 165 };
const NODES = {
  pv: { x: 200, y: 48, label: "Solar" },
  grid: { x: 62, y: 165, label: "Rețea" },
  battery: { x: 338, y: 165, label: "Baterie" },
  load: { x: 200, y: 282, label: "Consumatori" },
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
    return {
      type: "custom:goodwe-energy-flow-card",
      pv_power: "sensor.goodwe_ems_pv_power",
      load_power: "sensor.goodwe_ems_load_power",
      grid_power: "sensor.goodwe_ems_grid_active_power",
      battery_power: "sensor.goodwe_ems_battery_power",
      battery_soc: "sensor.goodwe_ems_battery_soc",
      pzu_price: "sensor.goodwe_ems_pzu_price",
      monthly_profit: "sensor.castig_lunar",
    };
  }

  setConfig(config) {
    if (!config) throw new Error("Configurație lipsă");
    this._config = { ...DEFAULTS, ...config };
    this._rendered = false;
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
    this._update();
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
        .rail { stroke: var(--divider-color, #d0d0d0); stroke-width: 2.2; fill: none; }
        .flow {
          stroke: var(--flow-color, var(--primary-color)); stroke-width: 2.6;
          fill: none; stroke-linecap: round;
          stroke-dasharray: 5 13;
          animation: dash linear infinite;
        }
        @keyframes dash { to { stroke-dashoffset: -18; } }
        .flow.hidden { display: none; }
        .icon { fill: none; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
        .node { cursor: pointer; }
        .node:hover .ring { stroke-width: 2; }
        .ring { fill: var(--card-background-color, #fff); stroke: var(--divider-color, #d0d0d0); stroke-width: 1.4; }
        .value {
          font-size: 12.5px; font-weight: 500; text-anchor: middle;
          fill: var(--primary-text-color); font-variant-numeric: tabular-nums;
        }
        .label {
          font-size: 10.5px; text-anchor: middle;
          fill: var(--secondary-text-color);
        }
        .soc { font-size: 10px; text-anchor: middle; fill: var(--secondary-text-color); }
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
        <svg viewBox="0 0 400 330" xmlns="http://www.w3.org/2000/svg">
          ${this._rails()}
          ${this._flows()}
          ${this._node("pv")}
          ${this._node("grid")}
          ${this._node("battery")}
          ${this._node("load")}
          ${this._hub()}
        </svg>
        <div class="footer">
          <span>Câștig luna curentă</span>
          <span class="profit" id="profit">—</span>
        </div>
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
          `<path class="flow hidden" id="flow-${key}" d="${this._path(n)}" />`
      )
      .join("");
  }

  _path(node) {
    // Traseu în „L" rotunjit: pe axa dominantă întâi, apoi cot spre hub.
    if (node.x === HUB.x) return `M ${node.x} ${node.y} L ${HUB.x} ${HUB.y}`;
    return `M ${node.x} ${node.y} L ${HUB.x} ${HUB.y}`;
  }

  _node(key) {
    const n = NODES[key];
    const below = key === "load";
    const valueY = below ? n.y + 46 : n.y - 34;
    const labelY = below ? n.y + 58 : n.y - 22;
    return `
      <g class="node" id="node-${key}">
        <circle class="ring" cx="${n.x}" cy="${n.y}" r="27" />
        <g class="icon" transform="translate(${n.x},${n.y})"
           stroke="var(--primary-text-color)" id="icon-${key}">
          ${ICONS[key]}
        </g>
        <text class="value" x="${n.x}" y="${valueY}" id="value-${key}">—</text>
        <text class="label" x="${n.x}" y="${labelY}">${n.label}</text>
        ${key === "battery" ? `<text class="soc" x="${n.x}" y="${n.y + 44}" id="soc">—</text>` : ""}
      </g>`;
  }

  _hub() {
    return `
      <g>
        <circle class="ring" cx="${HUB.x}" cy="${HUB.y}" r="30" />
        <g class="icon" transform="translate(${HUB.x},${HUB.y})" stroke="var(--primary-text-color)">
          ${ICONS.hub}
        </g>
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
    this._setFlow("pv", pv, false);
    this._setFlow("load", load, false);
    this._setFlow("grid", grid, grid < 0);
    this._setFlow("battery", battery, battery < 0);

    this._setValue("pv", pv);
    this._setValue("load", load);
    this._setValue("grid", grid);
    this._setValue("battery", battery);

    this._updateBatteryIcon(missing);
    this._updatePrice(missing);
    this._updateProfit(missing);

    const warn = this.shadowRoot.getElementById("warn");
    if (missing.length) {
      warn.style.display = "";
      warn.textContent = `Entități indisponibile: ${missing.join(", ")}`;
    } else {
      warn.style.display = "none";
    }
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
      soc < 15 ? "var(--error-color, #db4437)" : "var(--primary-color)"
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
    const el = this.shadowRoot.getElementById("profit");
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

customElements.define("goodwe-energy-flow-card", GoodweEnergyFlowCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "goodwe-energy-flow-card",
  name: "GoodWe Energy Flow Card",
  description: "Diagramă de flux energetic cu preț PZU orar și câștig lunar.",
  preview: true,
});

console.info(
  `%c GOODWE-ENERGY-FLOW-CARD %c v${CARD_VERSION} `,
  "color:#fff;background:#1D9E75;font-weight:500",
  "color:#1D9E75;background:transparent"
);
