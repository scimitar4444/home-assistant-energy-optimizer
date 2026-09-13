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

## Gemessene Netzladeblöcke

Die Netzladung läuft als eine kleine Zustandsmaschine und nicht als Folge
unabhängiger Neuplanungen. Ein Block kann nur für einen Slot beginnen, dessen
Start exakt auf einer Viertelstundengrenze liegt. Dann friert die Integration
die zusammenhängenden Lade-Slots mit bestätigten Preisen, den Startwert des
kumulativen DC-Batterieladezählers und ein DC-Energieziel ein. Dieses Ziel
ergibt sich aus der geplanten AC-Ladeenergie multipliziert mit dem
konfigurierten Ladewirkungsgrad. Spätere Änderungen der Last- oder PV-Prognose
verändern einen aktiven Block nicht.

Der Block endet, sobald die gemessene Zielenergie erreicht ist, sein
eingefrorenes Ende erreicht ist, der SoC 100 % erreicht oder die Regelung
deaktiviert wird. Ein ungültiger, zurückgesetzter oder stehengebliebener
kumulativer Ladezähler bricht den Block ausfallsicher ab. Herstellerspezifische
BMS-Signale zum zulässigen Ladestrom gehören bewusst nicht zum allgemeinen
Integrationsvertrag und müssen, sofern vorhanden, in der nachgelagerten
Schalt-Automation abgesichert werden.

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
- normale Befehle laufen spätestens an der aktuellen Viertelstundengrenze ab;
- während eines aktiven Netzladeblocks wird sein Befehl mit einer rollierenden
  Gültigkeit von höchstens sieben Minuten erneuert, jedoch nie über das
  eingefrorene Blockende hinaus;
- Planung und Schreiben sind getrennt. Die Installation der Integration allein
  kann den Wechselrichter nicht schalten.
