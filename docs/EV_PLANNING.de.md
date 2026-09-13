# Optionale Planung für Elektrofahrzeuge

Das optionale Fahrzeugmodul ist ein lokaler, herstellerneutraler
Abfahrtsplaner. Es ist standardmäßig deaktiviert. Die aktuelle Beta arbeitet
strikt **nur beobachtend**: Sie berechnet Empfehlungen, schreibt aber nichts an
die Wallbox und übernimmt einen noch nicht ausgeführten Ladeplan nicht in die
maßgebliche Batteriesteuerung.

## Grenze der aktuellen Beta

Das Modul verwendet derzeit:

- die Brutto-Standortlast einschließlich eines ladenden Autos in W;
- Wallbox-Leistung in W und fortlaufende Wallbox-Energie in kWh;
- einen Binärsensor „Fahrzeug verbunden“;
- den Fahrzeug-SoC von 0 bis 100 Prozent;
- einen eigenen Abfahrtskalender.

Eine tatsächliche Fahrzeugladung bleibt im gemeinsamen Standortmesswert
sichtbar. Der vorhandene zentrale Optimierer kann ein wirklich ladendes Auto
daher aus PV, Hausbatterie und/oder Netz mitversorgen – innerhalb der normalen
Hausreserve und Hardwaregrenzen. Die bloße Ladeempfehlung verändert diese
Verteilung nicht.

Ist die Fahrzeugplanung deaktiviert, bleibt der bisherige Haus- und
Batteriepfad unverändert und es werden keine EV-Sensoren angelegt.

## Messkonzept

Die gewählten Standort-Leistungssensoren müssen den **Bruttoverbrauch nach
PV- und Batterieflüssen** messen, nicht nur den Netzbezug. Sie müssen Haus und
Fahrzeug enthalten. Der Wallbox-Zähler ist nur der Fahrzeug-Unterzähler:

```text
Hausleistung = Brutto-Standortlast - Wallbox-Leistung
Hausenergie   = rekonstruierte Standortenergie - Wallbox-Energie
```

So bleibt eine echte Fahrzeugladung in der Live-Steuerung enthalten, wird aber
nicht als wiederkehrende Haushaltsgrundlast angelernt. Unpassende Einheiten,
veraltete Werte, falsche Vorzeichen oder widersprüchliche Messpfade werden
abgelehnt und nicht still auf null gesetzt. Hohe Fahrzeuglasten werden vor der
Subtraktion geprüft; erst danach gilt die normale Plausibilitätsgrenze für das
Haus.

Erforderliche Einheiten sind W für die Live-Leistung, kWh für den fortlaufenden
Wallbox-Energiezähler und Prozent für den Fahrzeug-SoC. Andere Einheiten
rechnet diese Beta nicht automatisch um.

## Abfahrtskalender

Verwende einen eigenen Kalender. Der Beginn jedes Termins mit Uhrzeit ist die
Abfahrtsfrist. Ganztagstermine werden ignoriert. Schreibe die benötigte Strecke
zeilenweise in die Terminbeschreibung:

```text
distance_km: 80
reserve_km: 40
```

`distance_km` ist Pflicht, muss größer als 0 sein und darf höchstens 2000
betragen. `reserve_km` ist optional, muss zwischen 0 und 1000 liegen und wird
sonst aus der Konfiguration übernommen. `:` und `=` sind zulässig. Der
Termintitel dient nur der Anzeige und wird nicht ausgewertet.

Die Beta liest bis zu 14 Tage voraus. Der früheste Termin mit Uhrzeit ist
maßgeblich; fehlt dort eine gültige Strecke, fordert das Modul eine Korrektur
an, statt still zum späteren Termin zu springen. Diese Abfahrt wird innerhalb
des 48-Stunden-Horizonts geplant. Eine Abfahrt zwischen 48 Stunden und
14 Tagen wird mit „Warte auf Planungszeitraum“ angezeigt; danach erscheint
„Keine anstehende Fahrt“. Direkt
aufeinanderfolgende Fahrten werden noch nicht gemeinsam berechnet; vor der
nächsten Abfahrt müssen ein aktueller SoC und eine realistische erneute
Anschlussmöglichkeit vorliegen.

Serienänderungen nimmst du im Kalender vor. Unterstützt der Kalender Ausnahmen,
kannst du einen Einzeltermin verschieben oder auslassen beziehungsweise die
künftige Serie ändern. Der Optimierer liest nur die daraus entstehenden
Einzeltermine.

## Zeitpunkt und Ladeleistung

- Standard für normales Laden sind 3,6 kW; der Wert ist einstellbar;
- der automatische Boost ist einstellbar, aber hart auf 11 kW begrenzt;
- 22 kW werden nie automatisch angefordert und liegen außerhalb dieser Beta.

Der Planer schützt zuerst die zusammengefasste Standortreserve und wählt danach
unterbrechbare Ladezeiten. Das konfigurierte Standortlimit ist eine
Beobachtungsschätzung und ersetzt keine Schutz-, Phasen-, Fahrzeug- oder
Wallboxgrenze.

11 kW werden nur empfohlen, wenn 3,6 kW bis zur Abfahrt nicht reichen oder ein
bestätigtes günstiges Intervall deutlich teurere **bestätigte** Energie ersetzt.
„Deutlich“ bedeutet in dieser Beta mindestens 5 ct/kWh. Bestätigte kostenlose
oder negative Preise bleiben erhalten. Positives Laden
wird aufgeschoben, wenn noch unbekannte spätere Intervalle genügend Zeit
bieten; nach Veröffentlichung der Tarifpreise wird neu geplant. Unbekannte
Preise begründen keinen wirtschaftlichen Boost.

Zeitfenster enden minutengenau an der Abfahrt. Sieben verbleibende Minuten
werden niemals als volles Viertelstundenintervall gerechnet.

## PV, Batterie und Netz

Verbleibende PV-Leistung ist nach dem prognostizierten Hausverbrauch nur eine
Zeitpräferenz. Sie verspricht dem Auto keine bestimmte Stromquelle. In der
aktuellen Beta gilt:

- Nur die gemessene, tatsächlich laufende Fahrzeuglast geht in die zentrale
  Standortsteuerung ein.
- Ein hypothetischer Ladeplan wird nicht zur Batterielast addiert.
- Es gibt weder einen zweiten Batterieregler noch einen negativen
  Netzsollwert-Trick.
- Veraltete oder widersprüchliche EV-Daten sperren die aktuelle Empfehlung,
  ohne den vorhandenen Hausoptimierer anzuhalten.

Solange das Fahrzeug nicht verbunden ist, bleibt die Machbarkeit vorläufig,
weil seine künftige Verfügbarkeit unbekannt ist. Ein späterer aktiver
Wallbox-Adapter benötigt atomare Start-/Stoppbefehle, kurze Gültigkeit,
phasengenaue Grenzen und einen klaren manuellen Vorrang, bevor die empfohlene
EV-Last genau einmal in den zentralen Standortplan aufgenommen werden darf.
