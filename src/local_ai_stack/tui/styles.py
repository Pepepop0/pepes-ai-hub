APP_CSS = """
Screen {
    layout: vertical;
    background: $surface;
    color: $text;
}

#top-navigation {
    height: 3;
    width: 100%;
    background: $panel;
    border-bottom: solid $primary;
}

#top-navigation Button {
    width: 1fr;
    height: 3;
    min-width: 14;
    border: none;
}

#workspace {
    height: 1fr;
    width: 100%;
}

#context-sidebar {
    width: 20;
    min-width: 16;
    height: 100%;
    padding: 1;
    background: $panel;
    border-right: solid $primary;
}

#sidebar-title {
    height: 2;
    color: $text-muted;
    text-style: bold;
}

.sidebar-menu {
    display: none;
    height: auto;
}

.sidebar-menu.active {
    display: block;
}

.sidebar-menu Button {
    width: 100%;
    min-width: 12;
    height: 3;
    margin-bottom: 1;
    content-align: left middle;
}

#main-content {
    width: 1fr;
    height: 100%;
    background: $surface;
}

.route-view {
    width: 100%;
    height: 100%;
    padding: 1;
    overflow-y: auto;
}

#installation-location-title {
    height: 2;
}

#installation-mode-label,
#installation-custom-label {
    margin-top: 1;
    color: $text-muted;
}

#installation-location-mode,
#installation-custom-root {
    width: 100%;
    max-width: 80;
}

#installation-location-error {
    min-height: 1;
    margin-top: 1;
    color: $error;
}

#installation-location-preview {
    height: auto;
    min-height: 3;
    margin-top: 1;
    padding: 1;
    border: round $secondary;
}

#installation-location-note {
    margin-top: 1;
    color: $text-muted;
}

#profile-actions {
    height: 3;
    margin-bottom: 1;
}

#profile-actions Button {
    width: 1fr;
    min-width: 8;
    height: 3;
}

#profile-details {
    height: 1fr;
    padding-top: 1;
}

#right-rail {
    /* Wide enough for the pet canvas plus the slot's border and padding,
       but allowed to shrink on a narrow terminal: the pet scales to fit. */
    width: 38;
    min-width: 20;
    height: 100%;
    padding: 1;
    background: $panel;
    border-left: tall $secondary;
}

#right-rail.narrow {
    width: 24;
}

#right-rail.collapsed {
    width: 12;
    min-width: 12;
}

#right-rail.hidden {
    display: none;
}

#right-rail.collapsed .rail-content {
    display: none;
}

.rail-heading {
    color: $text-muted;
    text-style: bold;
    margin-top: 1;
}

.rail-value {
    text-style: bold;
    margin-bottom: 1;
}

#pet-slot {
    /* Fixed and square on screen: a terminal cell is about half as wide as it
       is tall, so 31 cells by 15 rows reads as a square. No border and no
       label: the character is the only thing in the box. */
    width: 100%;
    height: 15;
    padding: 1;
    color: $text-muted;
}

#right-rail-toggle {
    dock: bottom;
    width: 100%;
    min-width: 8;
}

#footer-bar {
    height: 1;
    width: 100%;
    padding: 0 1;
    background: $primary-background;
    color: $text;
}

#footer-brand {
    width: 1fr;
}

#footer-version {
    width: auto;
    text-align: right;
}
"""
