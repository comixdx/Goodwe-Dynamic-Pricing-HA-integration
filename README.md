# GoodWe EMS

Integrare Home Assistant pentru controlul invertoarelor hibride GoodWe ET prin Modbus, cu dispecerizare a bateriei după prețul PZU.

Registrele provin din **GoodWe ARM 745 Modbus Protocol Map, revizia 28.03.2025**.

---

## Ce face

**Control invertor** — cele patru grupe de funcții din protocol:

| Funcție | Registre |
|---|---|
| Limitare export / anti-backflow | 47509, 47510, 46708 |
| Comandă încărcare/descărcare (EMS) | 47505, 47511, 47512 |
| Încărcare din rețea | modurile EMS `0x0004` / `0x000B`, plus 47545, 47546 |
| Limite și praguri | 45558–45567, 47531, 47532, 47533 |

**Registre citite** (blocuri, o dată pe ciclu):

| Bloc | Conținut |
|---|---|
| 35001–35015 | putere nominală, serie, model — citite o singură dată |
| 35105–35145 | putere PV1–PV4, putere totală invertor, activ/reactiv/aparent |
| 35169–35212 | consum backup, consum total, putere baterie 1, contoare energie, module |
| 35264–35268 | putere baterie 2, module pachet 2 |
| 35301–35304 | putere PV totală, număr canale MPPT |
| 37007–37009 | SOC, SOH |
| 37056–37077 | energie totală încărcată/descărcată, capacitate nominală BMS1 |
| 39074 | capacitate nominală BMS2 |
| 10473–10480 | energie încărcabilă / descărcabilă permisă de BMS |

**Telemetrie** — putere PV pe canal și totală, putere invertor, putere activă la contor, consum și consum backup, putere baterie pe pachet, SOC, SOH, capacitate nominală BMS, energie încărcabilă/descărcabilă și contoare de energie.

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

## Configurare

**Pasul 1 — conexiune.** Tip (`Modbus TCP`, `RTU peste TCP` sau `serial`), adresa, și adresa slave. Implicit GoodWe răspunde pe **247** (0xF7), la 9600 bps.

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

Capacitatea din configurare e doar o rezervă. Dacă BMS-ul răspunde la 37076 (și 39074, pe montaj cu două pachete), valoarea lui are prioritate. La fel SOC-ul: registrul 37007 e sursa preferată, iar senzorul extern rămâne opțional, ca plasă de siguranță pentru instalațiile pe care blocul BMS nu răspunde.

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

**Entitățile se descoperă singure.** Cardul citește registrul de entități, ia entitățile platformei `goodwe_ems` și le potrivește după cheia de traducere, nu după `entity_id`. Asta contează pentru că Home Assistant compune `entity_id`-ul din numele *tradus* al entității în momentul creării: pe o instanță în română senzorul de putere PV se numește `sensor.goodwe_ems_putere_pv`, nu `sensor.goodwe_ems_pv_power`. O listă scrisă de mână în YAML merge doar în limba în care a fost scrisă.

Orice câmp scris explicit are prioritate — descoperirea umple doar golurile:

```yaml
type: custom:goodwe-energy-flow-card
title: Flux energetic
battery_power: sensor.pbattery1        # senzorul integrării GoodWe oficiale
invert_battery: true                  # `pbattery1` e pozitiv la descărcare
monthly_profit: sensor.castig_lunar   # opțional, îl faci tu — vezi mai jos
min_flow_watts: 30
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

### Câștigul lunar nu vine din integrare

`monthly_profit` e singurul câmp al cardului care nu se leagă de un senzor al integrării. Motivul e că integrarea nu are de unde: nu citește un contor de energie exportată, iar registrele ei de energie numără încărcarea și descărcarea bateriei, nu injecția în rețea. Fără câmp, rândul „Câștig luna curentă" pur și simplu nu se desenează.

Dacă îl vrei, îl compui din contorul tău de export și din prețul mediu ponderat pe care îl publică integrarea:

```yaml
utility_meter:
  export_lunar:
    source: sensor.CONTORUL_TAU_DE_EXPORT   # kWh injectați în rețea
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

**Semnele.** Harta ARM 745 nu documentează convenția de semn pentru 35139 (putere activă la contor) și 35182 (putere baterie). Uită-te o dată la card cu bateria vizibil în încărcare și cu surplus injectat în rețea; dacă o săgeată arată invers, comută flagul corespunzător. Dacă folosești în schimb senzorii integrării GoodWe oficiale, `pbattery1` e pozitiv la descărcare, deci acolo îți trebuie `invert_battery: true`.

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
| `goodwe_ems.set_ems_mode` | scrie 47505 → 47512 → 47511, în ordinea cerută de invertor |
| `goodwe_ems.set_export_limit` | scrie parametrul, apoi activarea |
| `goodwe_ems.force_charge` | mod Charge-BAT, completare din rețea |
| `goodwe_ems.force_discharge` | mod Discharge-BAT |
| `goodwe_ems.stop_forcing` | revenire în autoconsum |
| `goodwe_ems.clear_economic_schedule` | golește sloturile 47515–47530 prin 47533 |

---

## Trei lucruri de știut înainte de a porni dispecerizarea

**Registrele 47511 și 47512 sunt volatile.** Protocolul le marchează `Save = N`: se pierd la repornirea invertorului. Motorul le rescrie la fiecare ciclu și citește înapoi ce a scris; o discordanță apare în jurnal. O automatizare care scrie o singură dată la începutul ferestrei va eșua tăcut la primul reboot.

**Prețurile învechite nu comandă nimic.** `PriceSeries.is_actionable()` verifică simultan două lucruri: că seria e pentru ziua curentă și că a fost descărcată în ultimele 18 ore. Dacă oricare cade, invertorul trece în autoconsum. Cel mai rău caz devine o zi fără arbitraj, în loc de o baterie care se descarcă după programul de ieri.

**Programul economic concurează cu EMS.** Dacă ai sloturi active în 47515–47530, rulează `clear_economic_schedule` o dată înainte de a porni dispecerizarea.

---

## De verificat înainte de producție

**Registrul 35001 (Rate Power).** Harta nu precizează unitatea. Pe ET se citește de regulă direct în W, dar verific-o printr-o citire efectivă înainte s-o folosești ca divizor pentru procente. Integrarea o expune doar ca atribut de dispozitiv, nu o folosește în calcule.

**Parserul OPCOM** (`OpcomSource._extract_values`) a fost construit dintr-o randare textuală a paginii, nu din DOM-ul real. Euristica e „prețurile sunt singurele celule cu zecimale, iar prima de pe rând e agregatul zilnic". Verific-o cu un test pe HTML salvat înainte de a te baza pe ea.

Până atunci ești protejat de validarea din `PriceSeries.__post_init__`: orice parsare care nu dă exact 92/96/100 valori plauzibile ridică `PriceError` și trece pe ENTSO-E, în loc să întoarcă o serie coruptă.

---

## Teste

```bash
pip install pytest
pytest tests/
```

Testele acoperă logica pură — validarea seriei, numărarea intervalelor la schimbarea orei, ferestrele de arbitraj, limitele BMS și decodarea blocurilor de telemetrie (cu un client Modbus simulat) — fără să aibă nevoie de Home Assistant sau de un invertor.

---

## Licență

MIT
