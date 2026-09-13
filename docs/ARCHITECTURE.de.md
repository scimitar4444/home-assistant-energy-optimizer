# Architektur

Deutsch · [English](ARCHITECTURE.md)

```text
HA-Entitäten und Recorder-Statistiken
                  │
                  ▼
 Adapter + robuste Last-/PV-Prognose
                  │
                  ▼
 deterministischer Viertelstunden-Optimierer
                  │
                  ▼
 lokalisierte Sensoren + ablaufender Befehl
                  │
                  ▼
 optionale, nutzereigene Schalt-Automation
```

Das dynamische Programm minimiert Netzstromkosten und optionalen
Batterieverschleiß. Dabei berücksichtigt es SoC, Wirkungsgrade und
Leistungsgrenzen. Es verwendet keine gelernten Gewichte, kein neuronales Netz
und keinen externen Inferenz-Endpunkt.

## Optionaler EV-Beobachtungsadapter

Das optionale EV-Subsystem liegt in der aktuellen Beta bewusst außerhalb des
verbindlichen Steuerpfads. Ein reiner Abfahrtsplaner erzeugt Empfehlungen. Ein
abgesicherter Unterzähler-Adapter entfernt gemessene EV-Energie aus der
gelernten Haushaltshistorie. Der tatsächliche EV-Verbrauch bleibt Teil der
Brutto-Live-Leistung des gesamten Standorts. Eine nicht ausgeführte Empfehlung
wird weder in die Batterieoptimierung übernommen noch als Wallbox-Befehl
ausgegeben.

## Grenze der bestätigten Preise

Tarifanbieter veröffentlichen meist nur einen begrenzten bestätigten
Zeithorizont. Die Integration darf den Rest ihrer 48-Stunden-Anzeige mit
historischen Sieben-Tage-Mittelwerten füllen. Aktive Speicheraktionen werden
an der Grenze jedoch auf den Zustand einer passiven Referenzplanung begrenzt.
Energie darf daher nicht allein gekauft oder umgeleitet werden, um sie in einen
Zeitraum mit geschätztem Preis mitzunehmen. Entladen, günstiges Nachladen und
späteres erneutes Entladen innerhalb eines Tages bleiben möglich, wenn alle
dafür maßgeblichen Preise bestätigt sind.

## Fehlermodell

- ungültige SoC-Werte, einschließlich des verbreiteten Sentinelwerts `65535`,
  werden abgewiesen;
- die Datenabdeckung ist eine Diagnose und niemals ein prozentualer
  Hauptschalter;
- jede Aktion benötigt ihre konkreten Live- und Preiseingaben; fehlen diese,
  liefert die Integration `DEGRADED`, statt einen alten Befehl beizubehalten;
- `DISCHARGE` benötigt einen bestätigten aktuellen Preis;
- `PV_SURPLUS` benötigt gültige aktuelle Live-Leistungswerte;
- `PV_STORE` und `GRID_CHARGE` benötigen gültige Live-Leistungswerte, einen
  bestätigten aktuellen Preis, einen positiven Netzsollwert sowie eine spätere
  geplante Batterieentladung zu einem höheren Preis innerhalb des
  zusammenhängenden bestätigten Preishorizonts;
- `RESERVE` benötigt einen bestätigten aktuellen Preis und dieselbe spätere
  höherpreisige Entladung; bei einem aktuellen Preis kleiner oder gleich null
  ist das Halten der Energie bereits für sich bestätigt begründet;
- positive Netzsollwerte benötigen einen bestätigten aktuellen Preis und
  begrenzte Live-Messwerte;
- jeder Befehl läuft spätestens an der aktuellen Viertelstundengrenze ab;
- Planung und Schreiben sind getrennt. Die Installation der Integration allein
  kann den Wechselrichter nicht schalten.
