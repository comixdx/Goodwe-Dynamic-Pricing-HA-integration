# GoodWe EMS

Integrare Home Assistant pentru controlul invertoarelor hibride GoodWe, cu dispecerizare a bateriei după prețul PZU.

Comunicația e făcută de biblioteca [`goodwe`](https://pypi.org/project/goodwe/) — aceeași pe care o folosește integrarea GoodWe oficială din Home Assistant. De acolo vin detectarea automată a familiei (ET/EH/BT/BH, ES/EM/BP, DT/MS/NS/XS), alegerea portului și harta de registre. Peste ea, integrarea asta adaugă comanda EMS și dispecerizarea pe preț, care lipsesc din cea oficială.

Registrele pe care biblioteca nu le numește (armarea EMS în 47505, 46708, 45558–45567, 47531–47533, blocul BMS) sunt accesate prin pseudo-setările `modbus_<adresă>`, tot ale ei. Sursa lor: **GoodWe ARM 745 Modbus Protocol Map, revizia 28.03.2025**.

---

## Ce face

**Control invertor:**

| Funcție | Cum |
|---|---|
| Limitare export / anti-backflow | setările `grid_export`, `grid_export_limit`, plus 46708 |
| Comandă încărcare/descărcare (EMS) | armare în 47505, apoi `ems_mode` / `ems_power_limit` |
| Încărcare din rețea | modurile EMS `IMPORT_AC` / `CHARGE_BATTERY`, plus `fast_charging` |
| Limite și praguri | 45558–45567, 47531, 47532, 47533 |
| Mod de funcționare | `work_mode` (General, Back-up, Eco, Peak shaving…) |

**Telemetrie** — nu mai e o listă scrisă de mână. Entitățile se generează din `inverter.sensors()`, deci fiecare model expune exact senzorii pe care îi are, cu numele și unitățile declarate de bibliotecă.

**Diagnostice** — *Descarcă diagnosticele* de pe pagina dispozitivului dă starea completă: familie, firmware, toate setările citite, datele runtime, decizia de dispecerizare și starea seriei de prețuri.

**Prețuri PZU** — serie de 96 de intervale de 15 minute (92 sau 100 în zilele de schimbare a orei), de la OPCOM, cu ENTSO-E ca rezervă. Plus prețul mediu ponderat lunar, care e baza de decontare a prosumatorului conform Ordinului ANRE 15/2022.

**Dispecerizare pe preț** — motorul caută fereastra ieftină de încărcare și fereastra scumpă de descărcare, verifică dacă marja acoperă costul de ciclare, și rescrie comanda EMS la fiecare ciclu.

**Card Lovelace** — diagramă de flux energetic cu animație, preț PZU curent și câștig lunar. E servit din integrare, deci nu ai nevoie de un al doilea repo HACS, și își găsește singur entitățile.

---

## Instalare

**HACS** → Integrations → meniul din colț → Custom repositories → adaugă URL-ul acestui repo, categoria *Integration* → instalează → repornește Home Assistant.

Pentru a publica repo-ul pe GitHub cu tot cu release-ul pe care îl citește HACS, rulează `./publish.sh` (cere [GitHub CLI](https://cli.github.com) autentificat). Scriptul completează singur `codeowners`, `documentation` și `issue_tracker` în `manifest.json` cu utilizatorul tău.

**Manual** — copiază `custom_components/goodwe_ems/` în `config/custom_components/` și repornește.

Apoi *Settings → Devices & Services → Add Integration → GoodWe EMS*.

---

## Upgrade de la 1.x

Versiunea 2.0 schimbă modul de comunicație: în loc de pymodbus și o hartă de registre scrisă de mână, se folosește biblioteca `goodwe`. Ce înseamnă asta la upgrade:

- **Intrarea de configurare se migrează singură.** Adresa IP se păstrează, restul (tip conexiune, port, adresă slave) se aruncă și se re-detectează. Parametrii de baterie și dispecerizare rămân neatinși. Dacă invertorul doarme în momentul migrării, Home Assistant reîncearcă la următoarea pornire.
- **Entitățile de telemetrie se refac.** Ele vin acum din biblioteca `goodwe`, deci au alte `unique_id`-uri și alte `entity_id`-uri, iar setul e mai bogat (tensiuni, curenți, temperaturi, MPPT). Cele vechi rămân în registru ca *restricted*; le ștergi din *Settings → Devices & Services → Entities*, filtrând după *Status: Unavailable*. Automatizările și tablourile care le foloseau trebuie repuse pe noile entități.
- **Entitățile proprii integrării își păstrează cheile** — prețul PZU, starea dispecerizării, comutatorul de dispecerizare — dar și lor li se schimbă `unique_id`-ul, fiindcă e acum legat de seria invertorului, nu de intrarea de configurare.
- **Conexiunile seriale și RTU peste TCP dispar.** Vezi nota de la pasul 1 al configurării.

---

## Configurare

**Pasul 1 — conexiune.** Doar adresa IP a invertorului. Portul (UDP 8899 pentru un dongle Wi-Fi/LAN, Modbus TCP 502 pentru un modul Ethernet), familia și adresa de comunicație sunt detectate automat.

> **Conexiunile seriale nu mai sunt posibile.** Biblioteca `goodwe` vorbește doar UDP și Modbus TCP, deci un adaptor RS485 legat direct la mașina cu Home Assistant, sau un gateway serial-Ethernet care face RTU peste TCP (Elfin, USR, Waveshare), nu mai funcționează. Dacă asta e situația ta, rămâi pe versiunea 1.4.0.

**Pasul 2 — baterie și dispecerizare.**

| Câmp | Ce înseamnă |
|---|---|
| Capacitate utilă | kWh efectiv utilizabili, nu capacitatea nominală |
| Putere max. încărcare/descărcare | limitele fizice ale bateriei tale |
| SOC minim / țintă | rezerva pe care n-o atingi și nivelul până la care încarci |
| Randament dus-întors | tipic 0,88–0,92 pentru LiFePO4 |
| Cost de ciclare | uzura per MWh ciclat; sub acest prag arbitrajul e în pierdere |
| Senzor SOC | vezi mai jos |
| Token ENTSO-E | opțional, se cere gratuit la transparency@entsoe.eu |
| Păstrează energia pentru vârf | vezi mai jos |

### Capacitatea și SOC-ul se citesc singure

Capacitatea din configurare e doar o rezervă. Dacă BMS-ul răspunde la 37076, valoarea lui are prioritate. La fel SOC-ul: senzorul `battery_soc` al bibliotecii e sursa preferată, iar senzorul extern rămâne opțional, ca plasă de siguranță pentru instalațiile pe care blocul BMS nu răspunde.

### Cum decide motorul cât poate încărca

Două constrângeri diferite, aplicată cea mai strânsă:

- **Politica ta** — `(SOC țintă − SOC) × capacitate` la încărcare, `(SOC − SOC minim) × capacitate` la descărcare.
- **BMS-ul, acum** — registrele 10476 și 10478 raportează energia pe care pachetul o acceptă în acest moment, cu derating de temperatură și limite de celulă deja incluse.

A doua e mai bună decât aritmetica pe SOC, care presupune o baterie ideală. Într-o dimineață de iarnă, `capacitate × SOC` promite 6 kWh de încărcare, iar BMS-ul acceptă 1,5 kWh; motorul planifică fereastra pe 1,5.

### Păstrarea energiei pentru vârf

Implicit, între încărcarea ieftină și vârful de seară bateria rămâne în autoconsum: alimentează casa cu energia cumpărată la 180 lei în loc s-o vândă la 1250. Cu opțiunea activată, în intervalul dintre ele bateria trece în standby și casa trage din rețea, iar energia se păstrează pentru vârf.

Nu e gratuit: plătești consumul de peste zi la prețul zilei. Merită doar dacă vârful bate prețul curent cu cel puțin costul de ciclare — condiție pe care motorul o verifică singur la fiecare ciclu, deci în zilele plate opțiunea nu face nimic.

Există o capcană: pe modelele care nu populează 10476/10478, registrele întorc zero fără eroare, iar un zero luat de bun ar bloca dispecerizarea permanent. Filtrul e simplu — o baterie nu poate fi simultan plină și goală, deci dacă *ambele* ies zero, ambele sunt marcate indisponibile și se cade înapoi pe politica ta. Zero pe unul singur e credibil și se respectă.

---

## Cardul

Nu apare singur pe niciun tablou: integrarea doar îl servește, tu îl adaugi o dată. *Tablou → Editează → Adaugă card*, caută **GoodWe Energy Flow Card**. Dacă tocmai ai instalat integrarea, reîncarcă pagina întâi — scriptul se injectează la pornirea Home Assistant, iar frontendul ține pagina în cache.

Atât e de ajuns:

```yaml
type: custom:goodwe-energy-flow-card
title: Flux energetic
```

**Entitățile se descoperă singure.** Cardul citește registrul de entități, ia entitățile platformei `goodwe_ems` și le potrivește după `unique_id`, nu după `entity_id`. Asta contează pentru că Home Assistant compune `entity_id`-ul din numele entității în momentul creării, iar acela se traduce. `unique_id`-ul are forma `goodwe_ems-{cheie}-{serie}`, iar cheile sunt id-urile de senzor ale bibliotecii: `ppv`, `house_consumption`, `active_power`, `pbattery1`, `battery_soc`.

Orice câmp scris explicit are prioritate — descoperirea umple doar golurile:

```yaml
type: custom:goodwe-energy-flow-card
title: Flux energetic
monthly_profit: sensor.castig_lunar   # opțional, îl faci tu — vezi mai jos
min_flow_watts: 30
invert_battery: false                 # comută dacă săgeata bateriei arată invers
```

**Cu două invertoare** cardul ar amesteca entitățile, așa că ia un singur grup: cel mai complet. Pentru al doilea, dă-i intrarea de configurare — `config_entry_id`, din URL-ul paginii integrării (`.../config/integrations/integration/goodwe_ems` → click pe intrare):

```yaml
type: custom:goodwe-energy-flow-card
config_entry_id: 01JD7Q...
```

Când un câmp rămâne fără entitate, cardul scrie sub diagramă exact care — nu desenează zero în tăcere.

### Invertorul din mijloc

Schema e un romb, ca la cardul *Energy distribution* din Home Assistant: soare sus, rețea stânga, consumatori dreapta, baterie jos, fiecare nod cu inelul lui colorat și cu puterea scrisă înăuntru. Diferența e nodul din centru: cardul din Home Assistant lucrează cu energie contorizată, unde nu contează pe unde trece, iar liniile se întâlnesc într-un simplu punct. Aici contează — la un invertor hibrid fiecare kilowatt trece fizic prin invertor, iar o săgeată directă PV → casă ar arăta un traseu care nu există.

Dacă vrei totuși exact aspectul din Home Assistant, scoate-l:

```yaml
type: custom:goodwe-energy-flow-card
show_inverter: false
```

Culorile inelelor se pot rescrie din temă, dacă nu-ți place paleta implicită:

```yaml
card_mod:
  style: |
    :host {
      --goodwe-pv-color: #f5a623;
      --goodwe-grid-color: #5a9fd4;
      --goodwe-load-color: #3fc4c4;
      --goodwe-battery-color: #e5559f;
    }
```

### Tabloul Energy

*Settings → Dashboards → Energy*. Cele patru secțiuni se completează cu:

| Secțiune | Senzor |
| --- | --- |
| Grid consumption | `meter_e_total_imp` (*Meter Total Energy (import)*) |
| Return to grid | `meter_e_total_exp` (*Meter Total Energy (export)*) |
| Solar production | `e_total` (*Total PV Generation*) |
| Battery in / out | `e_bat_charge_total` / `e_bat_discharge_total` |

Dacă lista de selecție apare goală, senzorul nu îndeplinește [condițiile din FAQ-ul Energy](https://www.home-assistant.io/docs/energy/faq/#troubleshooting-missing-entities): domeniul `sensor`, `device_class` energie, `state_class` `total` sau `total_increasing` și unitatea kWh. Biblioteca declară unitatea kWh pentru toți cei de mai sus, iar integrarea le pune automat clasa și `state_class`-ul potrivite.

**Senzorii de putere nu apar în listă și nu e o defecțiune.** `ppv`, `active_power` și `house_consumption` sunt wați cu `state_class: measurement`, ceea ce e corect pentru ce sunt; tabloul Energy consumă exclusiv kWh acumulați. Pentru putere instantanee ai cardul de flux.

Prețul îl legi tot de aici: la *Grid consumption → Use an entity with current price* alege `pzu_price`, care e deja în RON/kWh.

### Câștigul lunar nu vine din integrare

`monthly_profit` rămâne singurul câmp al cardului fără senzor gata făcut: integrarea publică acum kilowații injectați, dar nu și leii. Fără câmp, rândul „Câștig luna curentă" pur și simplu nu se desenează.

Îl compui din contorul de export al integrării și din prețul mediu ponderat pe care tot ea îl publică:

```yaml
utility_meter:
  export_lunar:
    source: sensor.goodwe_ems_meter_total_energy_export    # kWh injectați
    cycle: monthly

template:
  - sensor:
      - name: Câștig lunar
        unique_id: goodwe_castig_lunar
        unit_of_measurement: RON
        state_class: measurement
        state: >
          {% set kwh = states('sensor.export_lunar') | float(0) %}
          {% set lei_mwh = states('sensor.goodwe_ems_pret_mediu_ponderat_lunar') | float(0) %}
          {{ (kwh * lei_mwh / 1000) | round(2) }}
```

Aici `entity_id`-ul îl scrii tu, deci verifică-l în *Developer Tools → States*. Cel din exemplu e forma de pe o instanță în română; în engleză senzorul e `sensor.goodwe_ems_pzu_monthly_weighted_price`. Descoperirea automată acoperă doar câmpurile cardului, nu șabloanele tale.

**E o estimare, nu suma de pe factură.** OPCOM publică prețul mediu ponderat al unei luni abia la începutul lunii următoare, deci `pzu_monthly_weighted` conține luna trecută, iar `export_lunar` numără luna curentă. Cât timp luna e în curs, înmulțești kilowații de acum cu prețul de luna trecută. Valoarea se așază pe cea reală abia după ce OPCOM publică, iar Ordinul ANRE 15/2022 cere prețul lunii proprii. Formula e aceeași cu `monthly_settlement()` din `pzu_prices.py`.

**Semnele.** Convenția de semn pentru `active_power` (putere activă la contor) și `pbattery1` (putere baterie) diferă între modele și nu e documentată în harta ARM 745. Uită-te o dată la card cu bateria vizibil în încărcare și cu surplus injectat în rețea; dacă o săgeată arată invers, comută `invert_grid` sau `invert_battery`.

---

## Dacă apare „Custom element doesn't exist"

Cardul se înregistrează singur; nu trebuie adăugat manual la *Settings → Dashboards → Resources*. Când totuși nu apare, bisectează:

1. **Deschide `http://ADRESA_HA:8123/goodwe_ems/goodwe-energy-flow-card.js`.** 404 înseamnă că lipsește `custom_components/goodwe_ems/www/` de pe instanță — subfolderul se pierde des la copiere. Dacă fișierul se descarcă, înregistrarea a mers și problema e în browser.
2. **Testează într-o fereastră incognito.** Frontendul HA rulează un service worker care servește pagina din cache chiar și la `Ctrl+Shift+R`. Dacă în incognito apare, golește *DevTools → Application → Clear site data*.
3. **Caută în jurnal** linia `Cardul GoodWe Energy Flow este servit la ...`. Absența ei înseamnă că integrarea nu a pornit deloc.

Ca ultimă soluție, adaugă-l manual ca resursă Lovelace de tip *JavaScript Module*, cu URL-ul `/goodwe_ems/goodwe-energy-flow-card.js`. Dubla încărcare nu strică nimic — `customElements.define` e protejat.

## Servicii

| Serviciu | Efect |
|---|---|
| `goodwe_ems.set_ems_mode` | armează 47505, apoi scrie puterea și modul, în ordinea cerută de invertor |
| `goodwe_ems.set_export_limit` | scrie parametrul, apoi activarea |
| `goodwe_ems.force_charge` | mod EMS `CHARGE_BATTERY`, completare din rețea |
| `goodwe_ems.force_discharge` | mod EMS `DISCHARGE_BATTERY` |
| `goodwe_ems.stop_forcing` | revenire în autoconsum |
| `goodwe_ems.clear_economic_schedule` | golește sloturile programului economic prin 47533 |

Ultimele două există și ca butoane pe pagina dispozitivului, alături de *Sincronizează ceasul*.

---

## Trei lucruri de știut înainte de a porni dispecerizarea

**Registrele 47511 și 47512 sunt volatile.** Protocolul le marchează `Save = N`: se pierd la repornirea invertorului. Motorul le rescrie la fiecare ciclu și citește înapoi ce a scris; o discordanță apare în jurnal. O automatizare care scrie o singură dată la începutul ferestrei va eșua tăcut la primul reboot.

**Prețurile învechite nu comandă nimic.** `PriceSeries.is_actionable()` verifică simultan două lucruri: că seria e pentru ziua curentă și că a fost descărcată în ultimele 18 ore. Dacă oricare cade, invertorul trece în autoconsum. Cel mai rău caz devine o zi fără arbitraj, în loc de o baterie care se descarcă după programul de ieri.

**Programul economic concurează cu EMS.** Dacă ai sloturi active în 47515–47530, rulează `clear_economic_schedule` o dată înainte de a porni dispecerizarea.

---

## De verificat înainte de producție

**Parserul OPCOM** (`OpcomSource._extract_values`) a fost construit dintr-o randare textuală a paginii, nu din DOM-ul real. Euristica e „prețurile sunt singurele celule cu zecimale, iar prima de pe rând e agregatul zilnic". Verific-o cu un test pe HTML salvat înainte de a te baza pe ea.

Până atunci ești protejat de validarea din `PriceSeries.__post_init__`: orice parsare care nu dă exact 92/96/100 valori plauzibile ridică `PriceError` și trece pe ENTSO-E, în loc să întoarcă o serie coruptă.

---

## Teste

```bash
pip install pytest pytest-asyncio goodwe beautifulsoup4
pytest tests/
```

Testele acoperă logica pură — validarea seriei de prețuri, numărarea intervalelor la schimbarea orei, ferestrele de arbitraj, limitele BMS — plus stratul de control EMS cu un invertor simulat: armarea în 47505, ordinea scrierilor, citirea de verificare și recombinarea valorilor pe 32 de biți. Nu au nevoie de Home Assistant și nici de un invertor real.

---

## Licență

MIT
