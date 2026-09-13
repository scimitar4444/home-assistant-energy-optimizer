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
- PV‑Restprognose für heute und morgen, ersatzweise saisonale Historie;
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

Die Einrichtung führt durch Batterie/Tarif, Preise/Prognosen, Live‑Leistung, Langzeit‑Energiezähler und optionale Daten. Lasse zunächst Netzladen deaktiviert sowie Steuerungshelfer und Victron‑Host leer. Beobachte einige Tage die Prognose und den tatsächlichen Netzbezug. Erst danach sollte eine eigene Automation die kurzlebigen Steuerbefehle übernehmen.

Die Zustände bedeuten:

| Zustand | Bedeutung |
|---|---|
| `DISCHARGE` | Batterieeinsatz ist jetzt wirtschaftlich |
| `RESERVE` | Energie für ein wertvolleres bekanntes Intervall halten |
| `PV_SURPLUS` | PV versorgt das Haus, Überschuss lädt die Batterie |
| `PV_STORE` | günstiges bekanntes Netz versorgt das Haus, PV lädt die Batterie |
| `GRID_CHARGE` | günstigen bekannten Netzstrom für ein teures bekanntes Intervall laden |
| `DEGRADED` | Datenqualität reicht nicht; sicherer Ersatzbefehl |

Planung und Schalten sind absichtlich getrennt. Befehle enden spätestens an der nächsten Viertelstundengrenze. Geschätzte Folgepreise dürfen eine Tendenz zeigen, aber niemals allein Netzladen oder PV‑Umlenkung freigeben. Details stehen in der [Architektur](docs/ARCHITECTURE.md); Beispielkarten liegen unter [`examples/`](examples/).

## Entwicklung

```bash
python -m unittest discover -s tests -v
python -m compileall -q custom_components tests
```

## Lizenz

MIT, siehe [LICENSE](LICENSE).
