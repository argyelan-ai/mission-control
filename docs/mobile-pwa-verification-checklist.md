# Mobile/PWA Verifikations-Checkliste

> Pflicht-Check für alle Mobile-spezifischen Tasks. Bei Browser-Tool-Timeout: Manuelles Review mit Screenshots nachreichen.

## 1. Viewport-Verifikation

**Mobile-Viewport einstellen:**
- Breite: 390px (iPhone 14 Pro)
- Höhe: 844px
- Device Pixel Ratio: 3

**Prüfung:**
- [ ] Content füllt Viewport ohne horizontalen Scroll
- [ ] Kein gequetschter Content
- [ ] Touch-Targets min. 44x44px (Apple HIG)

**Screenshots:**
```
Browser → DevTools → Device Toolbar → iPhone 14 Pro
Oder: agent-browser set viewport 390 844
```

---

## 2. Responsive Breakpoints

**MC Breakpoints (Tailwind):**
| Breakpoint | Min-Width | MobileNav | Desktop-Sidebar |
|------------|-----------|-----------|-----------------|
| `sm` | 640px | sichtbar | versteckt |
| `md` | 768px | sichtbar | versteckt |
| `lg` | 1024px | **versteckt** | **sichtbar** |
| `xl` | 1280px | versteckt | sichtbar |

**Code-Referenz:**
```tsx
// MobileNav.tsx
className="lg:hidden"  // Sichtbar bis 1024px

// AppShell.tsx
className="hidden lg:block"  // Desktop-Sidebar ab 1024px
```

**Prüfung:**
- [ ] MobileNav (Hamburger) sichtbar bei < 1024px
- [ ] Desktop-Sidebar sichtbar bei >= 1024px
- [ ] Keine Überlappung bei Breakpoint-Übergang

---

## 3. PWA-Manifest-Check

**Datei:** `frontend/src/app/manifest.ts`

**Pflichtfelder:**
- [ ] `name`: "Mission Control"
- [ ] `short_name`: "MC"
- [ ] `display`: "standalone" (App-ähnlich)
- [ ] `orientation`: "portrait"
- [ ] `theme_color` + `background_color`: #0A0A0A
- [ ] `icons`: /icon.svg (oder PNGs in 192x192, 512x512)

**Verifikation:**
```
DevTools → Application → Manifest
Oder: curl http://localhost/manifest.webmanifest | jq
```

---

## 4. Service-Worker-Check

**Datei:** `frontend/public/sw.js`

**Features:**
- [ ] `install` Handler mit `skipWaiting()`
- [ ] `activate` Handler mit Cache-Cleanup
- [ ] `fetch` Handler mit Network-First + Cache-Fallback
- [ ] API-Calls/Streams NICHT cachen

**Verifikation:**
```
DevTools → Application → Service Workers
Check: "Activated and running"
```

---

## 5. Safe-Area-Support (iOS Notch)

**Code-Referenz:**
```tsx
// MobileNav.tsx
top-[calc(env(safe-area-inset-top)+0.5rem)]
pb-[max(0.5rem,env(safe-area-inset-bottom))]
```

**Prüfung:**
- [ ] Hamburger-Button unterhalb Notch positioniert
- [ ] Bottom-Content nicht durch Home-Indicator verdeckt
- [ ] Funktioniert auf iPhone X+ (Notch) und iPhone SE (kein Notch)

---

## 6. Navigation-Flow

**Mobile (< 1024px):**
- [ ] Hamburger-Button sichtbar (oben links)
- [ ] Drawer öffnet bei Tap
- [ ] Backdrop bei Drawer-Open
- [ ] Navigation-Links klickbar
- [ ] Drawer schließt bei Link-Klick
- [ ] Drawer schließt bei Backdrop-Tap
- [ ] Logout funktioniert

**Desktop (>= 1024px):**
- [ ] Sidebar permanent sichtbar
- [ ] Hamburger-Button versteckt
- [ ] WorkspaceSwitcher sichtbar

---

## 7. Install-Prompt (A2HS)

**Voraussetzungen:**
- [ ] Manifest vorhanden
- [ ] Service Worker registriert
- [ ] HTTPS (oder localhost)
- [ ] Mindestens 2 Icons (Apple + Standard)

**Verifikation:**
```
DevTools → Application → Manifest → "Add to home screen"
iOS Safari → Share → "Add to Home Screen"
```

---

## 8. Evidence-Dokumentation

**Für jeden Mobile-Task:**

1. **Screenshots erstellen:**
   ```
   Browser-Viewport: 390x844px
   agent-browser screenshot --full
   ```

2. **Dateiname-Format:**
   ```
   /media/browser/{task-id}-mobile-{viewport}.jpg
   ```

3. **Im Task-Kommentar dokumentieren:**
   ```
   **Evidence** —
   - Screenshot: /media/browser/xxx.jpg (390x844px)
   - Viewport: 390x844
   - Breakpoint getestet: lg (1024px)
   - Safe-Area: iOS Notch berücksichtigt
   ```

---

## 9. Schnellreferenz

| Was zu prüfen | Wo | Wie |
|---------------|-----|-----|
| Viewport | Browser DevTools | 390x844px |
| Breakpoints | AppShell.tsx | lg:hidden / hidden lg:block |
| Safe-Area | MobileNav.tsx | env(safe-area-inset-*) |
| Manifest | /manifest.webmanifest | DevTools → Application |
| Service Worker | /sw.js | DevTools → Application |
| Install-Prompt | A2HS | iOS Safari Share |

---

## 10. Browser-Tool-Timeout Workaround

Wenn das Browser-Tool nicht verfügbar ist:

1. **Manuelle Verifikation:**
   - Entwickler öffnet DevTools manuell
   - Screenshots mit Device-Toolbar (iPhone 14 Pro)
   - Upload in `/media/browser/`

2. **Code-Review:**
   - Breakpoint-Klassen prüfen (`lg:hidden`)
   - Safe-Area-CSS prüfen (`env(safe-area-inset-*)`)
   - Manifest-Felder prüfen

3. **Im Kommentar dokumentieren:**
   ```
   **Note** — Browser-Tool timeout. Code-Review durchgeführt.
   Safe-Area: ✓ | Breakpoint: lg | Manifest: ✓
   ```

---

*Erstellt: 2026-03-08 | Task: 40bcdf73-cb6f-4ce1-8500-4f5bd9f6b29c*