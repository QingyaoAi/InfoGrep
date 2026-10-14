// The toolbar button opens the side panel beside the page. Everything else happens in
// the panel (sidepanel.js); nothing runs in any page until the user asks there.
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(console.error);
