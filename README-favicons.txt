Belvédère du Domaine — Pack favicons
=====================================

Tous ces fichiers vivent à la RACINE du site (même niveau qu'index.html).
Ne pas les déplacer dans un sous-dossier : les <link> du <head> les cherchent ici.

Fichiers
--------
- favicon.ico                   (16 / 32 / 48 / 64 px)
- favicon-16.png
- favicon-32.png
- apple-touch-icon.png          (180x180, fond opaque — iOS n'accepte pas la transparence)
- android-chrome-192x192.png
- android-chrome-512x512.png
- mstile-150x150.png
- safari-pinned-tab.svg         (monochrome, onglet épinglé Safari)
- site.webmanifest
- browserconfig.xml
- og-belvedere-1200x630.jpg     (image affichée lors des partages Facebook / WhatsApp)

Le motif
--------
L'icône reprend le pictogramme montagne du logo, en blanc plein sur fond anthracite
(#1B1B1D) avec l'arc rouge (#D0212B). Le logo complet est trop large pour tenir dans
un carré de 16 px : il devenait illisible. Seule la montagne est conservée.

Régénérer les icônes
--------------------
Les icônes sont fabriquées à partir de imagesdomaine/logo.png (version à fond
transparent du logo). Si le logo change, il faut refaire logo.png / logo-blanc.png
puis régénérer ce pack.

Après mise en ligne
-------------------
Les navigateurs gardent les favicons en cache très longtemps. Pour vérifier le
changement, ouvrir directement https://belvedere65.com/favicon.ico et forcer le
rechargement (Cmd+Maj+R), ou tester en navigation privée.
