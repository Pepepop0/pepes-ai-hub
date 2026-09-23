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
    /* No gap: five items must fit an 80x24 terminal. */
    margin-bottom: 0;
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

/* --- Dashboard ------------------------------------------------------------- */

#dashboard-overview {
    height: auto;
    margin-bottom: 1;
}

#dashboard-primary {
    height: 3;
    margin-bottom: 1;
}

#dashboard-primary Button {
    width: 100%;
    max-width: 40;
    min-width: 20;
}

#dashboard-secondary {
    height: 3;
    margin-bottom: 1;
}

#dashboard-secondary Button {
    width: 1fr;
    max-width: 20;
    min-width: 12;
    margin-right: 1;
}

#dashboard-operation,
#install-operation {
    height: auto;
    margin-top: 1;
}

#dashboard-notice {
    height: auto;
    min-height: 1;
    margin-top: 1;
}

#install-summary {
    height: auto;
    margin-bottom: 1;
}

#install-open-wizard {
    width: 100%;
    max-width: 40;
    min-width: 16;
}

/* Instalações: two actions per row, so a row fits a narrow terminal. */
.install-actions {
    height: auto;
}

.install-actions Button {
    width: 1fr;
    max-width: 30;
    min-width: 16;
    margin-right: 1;
}

#install-check {
    height: auto;
    margin-top: 1;
}

#install-known {
    margin-top: 1;
}

#install-known-list {
    height: auto;
    max-height: 12;
}

/* Instalar */
#new-install-summary {
    height: auto;
    margin-bottom: 1;
}

#new-install-start {
    width: 100%;
    max-width: 40;
    min-width: 16;
}

/* The empty Dashboard's invitation. */
#dashboard-empty {
    height: auto;
    margin-bottom: 1;
}

#dashboard-install {
    width: 100%;
    max-width: 40;
    min-width: 20;
}

/* --- Dialogs --------------------------------------------------------------- */

WelcomeScreen,
SwitchInstallationScreen {
    align: center middle;
}

.dialog {
    width: 90%;
    max-width: 76;
    height: auto;
    max-height: 90%;
    overflow-y: auto;
    padding: 1 2;
    border: round $primary;
    background: $surface;
}

#welcome-body {
    margin: 1 0;
}

#switch-list {
    height: auto;
    max-height: 10;
    margin: 1 0 0 0;
}

#switch-empty,
#switch-hint {
    height: auto;
}

.dialog-actions {
    layout: grid;
    grid-size: 2;
    grid-gutter: 0 1;
    grid-rows: 3;
    height: auto;
    margin-top: 1;
}

.dialog-actions Button {
    width: 100%;
}

.service-card {
    height: auto;
    margin-bottom: 1;
    padding: 1;
    border: round $secondary;
}

/* --- Wizard ---------------------------------------------------------------- */

#wizard {
    layout: vertical;
    background: $surface;
    color: $text;
}

#wizard-frame {
    width: 100%;
    max-width: 96;
    height: 1fr;
    padding: 1 2;
}

#wizard-title {
    height: 2;
    color: $text-muted;
}

#wizard-steps {
    height: 1fr;
}

.wizard-pane {
    width: 100%;
    height: 100%;
    padding: 1;
}

.wizard-question {
    height: 2;
}

#wizard-profile-buttons,
#wizard-mode-buttons {
    height: 3;
    margin-bottom: 1;
}

.wizard-choice {
    width: 1fr;
    max-width: 24;
    min-width: 10;
    margin-right: 1;
}

#wizard-profile-summary,
#wizard-mode-summary {
    height: auto;
    min-height: 2;
    margin-bottom: 1;
}

#wizard-custom-root {
    width: 100%;
    max-width: 80;
}

#wizard-custom-error {
    min-height: 1;
    color: $error;
}

#wizard-custom-label,
#wizard-recent-label {
    margin-top: 1;
    color: $text-muted;
}

#wizard-custom-browse {
    width: auto;
    min-width: 20;
    margin-top: 1;
}

#wizard-recent-roots {
    width: 100%;
    max-width: 80;
}

#wizard-recent-empty {
    color: $text-muted;
}

#wizard-conflict-report {
    height: auto;
    margin-bottom: 1;
}

#wizard-conflict-choices {
    height: auto;
    margin-bottom: 1;
}

#wizard-conflict-buttons,
#wizard-confirm-buttons {
    height: 3;
    margin-bottom: 1;
}

#wizard-conflict-buttons Button,
#wizard-confirm-buttons Button {
    width: 1fr;
    max-width: 32;
    min-width: 12;
    margin-right: 1;
}

#wizard-confirm-text {
    height: auto;
    margin-bottom: 1;
}

#wizard-actions {
    height: 3;
    margin-top: 1;
}

#wizard-actions Button {
    min-width: 16;
    margin-right: 1;
}

#wizard-footer {
    height: 1;
    width: 100%;
    padding: 0 1;
    background: $primary-background;
    color: $text;
}
"""
