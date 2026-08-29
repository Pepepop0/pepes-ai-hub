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
    width: 24;
    min-width: 18;
    height: 100%;
    padding: 1;
    background: $panel;
    border-left: tall $secondary;
}

#right-rail.collapsed {
    width: 12;
    min-width: 12;
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
    min-height: 5;
    padding: 1;
    border: round $secondary;
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
