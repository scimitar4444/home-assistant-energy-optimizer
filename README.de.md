# Home-Assistant-Energieoptimierer für Home Assistant

Deutsch · [English](README.md)

Home Assistant Energy Optimizer ist ein lokaler, deterministischer 48‑Stunden‑Energieoptimierer für Home Assistant. Er verbindet dynamische Strompreise, PV‑Prognosen, die Verbrauchshistorie und den Batterieladezustand. Ziel sind möglichst geringe Netzstromkosten, ohne Energie pauschal für eine einzelne spätere Preisspitze zu blockieren.

Er verwendet **kein LLM, keine Cloud‑KI und keine Online‑Modellberechnung**. Alles wird lokal in Home Assistant berechnet und ist reproduzierbar.

> **Beta- und Sicherheitshinweis**  
> Die Integration startet als Beobachtungs- und Simulationswerkzeug. Verbinde Steuerbefehle erst nach Prüfung von Vorzeichen, Einheiten, Grenzen und Rückfallebene mit deinem Wechselrichter. Netzladen ist standardmäßig ausgeschaltet.

## Funktionen

- Planung von 192 Viertelstunden, also 48 Stunden;
- bekannte Tarifpreise werden vorrangig genutzt, fehlende Zeiten nur als Tendenz aus den letzten sieben Tagen ergänzt;
- gekaufter oder umgeleiteter Strom darf nicht allein mit unsicheren späteren Preisen begründet werden;
- getrennte Bilanz für PV → Haus, PV → Batterie, Batterie → Haus und Netz;
- einstellbare Batteriekapazität, harte Entladegrenze, Wirkungsgrade, Ladeleistung und optionaler Batterieverschleiß;
- robuste Grundlastprognose aus Home‑Assistant‑Langzeitstatistiken;
- jede Planung beginnt beim gemessenen Batterie‑SoC und berücksichtigt
  nutzbare Energie, Wirkungsgrad sowie Lade- und Entladegrenzen;
- PV‑Restprognose für heute und morgen; für fehlende spätere Tagessummen wird
  die saisonale Historie mit prognostizierter Sonnenhöhe, Bewölkung und Regen
  korrigiert;
- nicht schaltende, herstellerneutrale Schnittstelle für unterbrechbare Auto-,
  Klima- und Gerätelasten mit Leistungsgrenzen und Kalenderfrist;
- optionale, herstellerneutrale Fahrzeugplanung mit Kalenderfrist;
- deutsche und englische Sensorbezeichnungen;
- optionale, rückgelesene Victron‑Modbus‑TCP‑Dienste für erfahrene Nutzer.

## Voraussetzungen

- Home Assistant ab Version 2026.8;
- Batterie‑SoC in Prozent;
- dynamischer Preisverlauf mit Preisen in **EUR/kWh**;
- PV‑Restprognose heute und PV‑Prognose morgen in kWh;
- aktuelle PV‑, Netzbezugs‑ und Hausverbrauchsleistung in W;
- fünf fortlaufende Energiezähler in kWh: Netzbezug, Einspeisung, PV‑Erzeugung, Batterieladung und Batterieentladung.

Unterstützte Preisattribute:

- `data` mit `start_time` und `price_per_kwh`;
- Tibber‑ähnlich `today` / `tomorrow` mit `startsAt` und `total`;
- `raw_today` / `raw_tomorrow` mit `start` und `value`.

## Installation über HACS

1. In HACS **Benutzerdefinierte Repositories** öffnen.
2. `https://github.com/scimitar4444/home-assistant-energy-optimizer` als Typ **Integration** hinzufügen.
3. **Home Assistant Energy Optimizer** installieren und Home Assistant neu starten.
4. **Einstellungen → Geräte & Dienste → Integration hinzufügen → Home Assistant Energy Optimizer** öffnen.

Alternativ den Ordner `custom_components/energy_optimizer` nach `config/custom_components/` kopieren und Home Assistant neu starten.

## Sicherer Einstieg

Die Einrichtung führt durch fünf Grundschritte für Batterie/Tarif,
Preise/Prognosen, Live‑Leistung, Langzeit‑Energiezähler und optionale Daten.
Danach folgt die optionale, rein beobachtende Fahrzeugplanung. Lasse zunächst
Netzladen deaktiviert sowie Steuerungshelfer und Victron‑Host leer. Beobachte
einige Tage die Prognose und den tatsächlichen Netzbezug. Erst danach sollte
eine eigene Automation die kurzlebigen Steuerbefehle übernehmen.

Die Zustände bedeuten:

| Zustand | Bedeutung |
|---|---|
| `DISCHARGE` | Batterieeinsatz ist jetzt wirtschaftlich |
| `RESERVE` | Energie für ein wertvolleres bekanntes Intervall halten |
| `PV_SURPLUS` | PV versorgt das Haus, Überschuss lädt die Batterie |
| `PV_STORE` | günstiges bekanntes Netz versorgt das Haus, PV lädt die Batterie |
| `GRID_CHARGE` | günstigen bekannten Netzstrom für ein teures bekanntes Intervall laden |
| `DEGRADED` | eine aktionsbezogene Eingabe fehlt oder ist ungültig; sicherer Ersatzbefehl |

Planung und Schalten sind absichtlich getrennt. Normale Befehle enden spätestens an der nächsten Viertelstundengrenze; ein aktiver, gemessener Netzladeblock erhält stattdessen eine kurze rollende Gültigkeit, die nie über sein festes Ende hinausgeht. Geschätzte Folgepreise dürfen eine Tendenz zeigen, aber niemals allein Netzladen oder PV‑Umlenkung freigeben. Die prozentuale Datenabdeckung ist nur eine Diagnose; für die Befehlsfreigabe zählen stattdessen der aktuelle SoC, die Live‑Messwerte und die bestätigten Preise, die die jeweilige Aktion tatsächlich benötigt. Details stehen in der [Architektur](docs/ARCHITECTURE.de.md); Beispielkarten liegen unter [`examples/`](examples/).

Netzladen läuft als ein gemessener Block aus zusammenhängenden, bestätigten Viertelstunden. Nach dem Start können Prognoseänderungen ihn weder vergrößern noch unterbrechen. Der kumulative Batterieladezähler beendet ihn, sobald die geplante gespeicherte Energie angekommen ist; bis zum ursprünglichen Blockende bleibt der Auftrag gesperrt, damit ein träger SoC dieselbe Energie nicht doppelt bestellt. Herstellerspezifische BMS-Sperren muss der nachgeschaltete Hardware-Adapter zusätzlich durchsetzen.

Das Standardprofil für leises Netzladen begrenzt die geplante Batterieladung auf **0,8 kW** und den Ladestrombefehl des Aktors auf **15 A**: Montag bis Freitag von 23:00 bis 06:30 Uhr, am Samstag- und Sonntagmorgen bis 09:30 Uhr. Einmalige Kalenderausnahmen sind vorgesehen, werden derzeit aber noch nicht ausgewertet.

## Optionale Fahrzeugplanung

Das Fahrzeugmodul ist standardmäßig ausgeschaltet und arbeitet in dieser Beta
nur beobachtend. Es berechnet aus Fahrzeug‑SoC und Kalendertermin eine
Ladeempfehlung, schaltet aber keine Wallbox. Eine noch nicht ausgeführte
Fahrzeugplanung verändert daher auch keinen Batteriebefehl. Der tatsächlich
gemessene Fahrzeugverbrauch bleibt Teil der Live‑Standortlast.

Voraussetzung ist das richtige Messkonzept: Die gewählten Sensoren für die
Brutto‑Standortlast müssen **Haus und Wallbox gemeinsam** enthalten; es sind
keine reinen Netzbezugs‑Sensoren. Die Standortenergie wird zunächst aus den
fünf Flusszählern rekonstruiert. Erst danach zieht ein eigener
Wallbox‑Unterzähler die Fahrzeugenergie aus der gelernten Haushaltsgrundlast ab.

Verwende dafür einen eigenen EV‑Abfahrtskalender. Ein Termin mit Uhrzeit und
`distance_km: 80` in der Beschreibung gilt als Fahrt; optional überschreibt
`reserve_km: 40` die konfigurierte Reserve. Ganztagstermine werden ignoriert.
Die nächste Fahrt geht innerhalb des 48‑Stunden‑Horizonts in die Planung ein.
Die Empfehlung nutzt standardmäßig 3,6 kW und darf automatisch höchstens 11 kW
vorschlagen, wenn die normale Leistung bis zur Abfahrt nicht reicht. 22 kW
liegen außerhalb dieser Beta. Einheiten, Kalenderformat und Sicherheitsgrenzen
stehen unter [Optionale Fahrzeugplanung](docs/EV_PLANNING.de.md).

## Entwicklung

```bash
python -m unittest discover -s tests -v
python -m compileall -q custom_components tests
```

## Lizenz

MIT, siehe [LICENSE](LICENSE).
