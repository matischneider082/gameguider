"""Build, sign, install, and pin the native macOS `GameGuider.app` bundle (AppKit + WKWebView)."""

from __future__ import annotations

import importlib.metadata
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

from . import REPO
from .backends import PLUGIN_ROOT

APP_NAME = "GameGuider"
BUNDLE_ID = "app.gameguider.GameGuider"
INSTALL_COMMAND = f"curl -LsSf https://github.com/{REPO}/releases/latest/download/install.sh | sh"

# Ad and tracker networks the in-app browser blocks, so guide sites open without their ads. Domains, not pages:
# blocking a network covers every site that embeds it.
AD_DOMAINS = (
    "doubleclick.net", "googlesyndication.com", "googleadservices.com", "googletagservices.com", "adservice.google.com",
    "google-analytics.com", "adnxs.com", "amazon-adsystem.com", "taboola.com", "outbrain.com", "criteo.com", "criteo.net",
    "pubmatic.com", "rubiconproject.com", "openx.net", "casalemedia.com", "indexww.com", "moatads.com", "scorecardresearch.com",
    "quantserve.com", "quantcount.com", "adsrvr.org", "advertising.com", "yieldmo.com", "sharethrough.com", "teads.tv",
    "33across.com", "media.net", "smartadserver.com", "adform.net", "bidswitch.net", "lijit.com", "sovrn.com", "gumgum.com",
    "spotxchange.com", "springserve.com", "connatix.com", "primis.tech", "ezoic.net", "ezojs.com", "adthrive.com",
    "cafemedia.com", "mediavine.com", "playwire.com", "nitropay.com", "venatusmedia.com", "aniview.com", "vidazoo.com",
    "minutemedia-prebid.com", "adsafeprotected.com", "doubleverify.com", "3lift.com", "triplelift.com", "contextweb.com",
    "zemanta.com", "revcontent.com", "mgid.com", "pubnative.net", "inmobi.com", "unrulymedia.com", "freestar.com",
)
# Common ad slots left as empty boxes once their scripts are blocked.
AD_SELECTORS = ("ins.adsbygoogle", "[id^='div-gpt-ad']", "[id^='google_ads_iframe']", "iframe[src*='doubleclick.net']")


def content_rules() -> str:
    """WebKit content-blocker rules: block requests to AD_DOMAINS (and subdomains), hide their leftover slots."""
    rules = [{"trigger": {"url-filter": "^[^:]+://+([^:/]+\\.)?" + domain.replace(".", "\\.") + "[:/]"}, "action": {"type": "block"}}
             for domain in AD_DOMAINS]
    rules.append({"trigger": {"url-filter": ".*"}, "action": {"type": "css-display-none", "selector": ", ".join(AD_SELECTORS)}})
    return json.dumps(rules)

SWIFT_LAUNCHER = r"""import AppKit
import Foundation
import WebKit

let contentRules = #"__CONTENT_RULES__"#

func compileContentRules(_ done: @escaping (WKContentRuleList?, Error?) -> Void) {
    guard let store = WKContentRuleListStore.default() else { return done(nil, nil) }
    store.compileContentRuleList(forIdentifier: "GameGuiderAdBlock", encodedContentRuleList: contentRules) { list, error in
        DispatchQueue.main.async { done(list, error) }
    }
}

/// Guide sources open here instead of the system browser: one window, back/forward, ads blocked.
final class BrowserWindow: NSObject, WKNavigationDelegate, WKUIDelegate {
    let window: NSWindow
    let webView: WKWebView
    let address = NSTextField(labelWithString: "")
    var backButton: NSButton!
    var forwardButton: NSButton!
    var rulesReady = false
    var pending: URL?

    override init() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 800),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        webView = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        super.init()
        window.title = "GameGuider Browser"
        window.minSize = NSSize(width: 600, height: 400)
        window.isReleasedWhenClosed = false
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true

        backButton = toolButton("chevron.left", "Back", #selector(goBack))
        forwardButton = toolButton("chevron.right", "Forward", #selector(goForward))
        let reloadButton = toolButton("arrow.clockwise", "Reload", #selector(reloadPage))
        let external = NSButton(title: "Open in Browser", target: self, action: #selector(openExternally))
        external.bezelStyle = .rounded
        external.toolTip = "Open this page in your default browser"
        address.lineBreakMode = .byTruncatingMiddle
        address.textColor = .secondaryLabelColor
        address.isSelectable = true
        address.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        address.setContentHuggingPriority(.defaultLow, for: .horizontal)

        let bar = NSStackView(views: [backButton, forwardButton, reloadButton, address, external])
        bar.orientation = .horizontal
        bar.spacing = 8
        bar.edgeInsets = NSEdgeInsets(top: 6, left: 10, bottom: 6, right: 10)
        let root = NSView()
        for view in [bar, webView] as [NSView] {
            view.translatesAutoresizingMaskIntoConstraints = false
            root.addSubview(view)
        }
        NSLayoutConstraint.activate([
            bar.topAnchor.constraint(equalTo: root.topAnchor),
            bar.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            bar.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            webView.topAnchor.constraint(equalTo: bar.bottomAnchor),
            webView.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            webView.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])
        window.contentView = root
        window.center()
        refresh()

        compileContentRules { [weak self] list, error in
            guard let self = self else { return }
            if let list = list {
                self.webView.configuration.userContentController.add(list)
            } else {
                fputs("Ad blocking is off: \(String(describing: error))\n", stderr)
            }
            self.rulesReady = true  // load what was clicked while the rules compiled, blocked from its first request
            if let url = self.pending {
                self.pending = nil
                self.webView.load(URLRequest(url: url))
            }
        }
    }

    func toolButton(_ symbol: String, _ label: String, _ action: Selector) -> NSButton {
        let button = NSButton(image: NSImage(systemSymbolName: symbol, accessibilityDescription: label) ?? NSImage(),
                              target: self, action: action)
        button.bezelStyle = .texturedRounded
        button.toolTip = label
        return button
    }

    func open(_ url: URL) {
        window.makeKeyAndOrderFront(nil)
        if rulesReady {
            webView.load(URLRequest(url: url))
        } else {
            pending = url
        }
    }

    @objc func goBack() { webView.goBack() }
    @objc func goForward() { webView.goForward() }
    @objc func reloadPage() { webView.reload() }
    @objc func openExternally() {
        if let url = webView.url ?? pending { NSWorkspace.shared.open(url) }
    }

    func refresh() {
        address.stringValue = webView.url?.absoluteString ?? pending?.absoluteString ?? ""
        let title = webView.title ?? ""
        window.title = title.isEmpty ? "GameGuider Browser" : title
        backButton.isEnabled = webView.canGoBack
        forwardButton.isEnabled = webView.canGoForward
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) { refresh() }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { refresh() }

    // Links that ask for a new window open in this one.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { webView.load(URLRequest(url: url)) }
        return nil
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        // Web pages only; mailto:, app links and the like go to the system.
        if let url = navigationAction.request.url, let scheme = url.scheme?.lowercased(),
           !["http", "https", "about", "blob", "data"].contains(scheme) {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKDownloadDelegate {
    let defaultBin: String
    let venvBinDir: String
    let smokeMode: Bool
    var serverProcess: Process?
    var window: NSWindow?
    var webView: WKWebView?
    var browser: BrowserWindow?
    var loadedURL: String = ""

    init(defaultBin: String, venvBinDir: String) {
        self.defaultBin = defaultBin
        self.venvBinDir = venvBinDir
        self.smokeMode = ProcessInfo.processInfo.environment["GAME_GUIDE_APP_SMOKE"] == "1"
        super.init()
    }

    func home() -> String {
        ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()  // $HOME first, so a test can point it elsewhere
    }

    func resolveExecutable() -> String {
        let env = ProcessInfo.processInfo.environment
        if let custom = env["GAME_GUIDE_BIN"], !custom.isEmpty, FileManager.default.isExecutableFile(atPath: custom) {
            return custom
        }
        if FileManager.default.isExecutableFile(atPath: defaultBin) {
            return defaultBin
        }
        let homeBin = (home() as NSString).appendingPathComponent(".local/bin/game-guide")
        if FileManager.default.isExecutableFile(atPath: homeBin) {
            return homeBin
        }
        return defaultBin
    }

    func buildMenu() {
        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit GameGuider", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appMenuItem.submenu = appMenu

        let editMenuItem = NSMenuItem()
        mainMenu.addItem(editMenuItem)
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(NSMenuItem.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editMenuItem.submenu = editMenu

        let viewMenuItem = NSMenuItem()
        mainMenu.addItem(viewMenuItem)
        let viewMenu = NSMenu(title: "View")
        let reloadItem = NSMenuItem(title: "Reload", action: #selector(reloadPage(_:)), keyEquivalent: "r")
        reloadItem.target = self
        viewMenu.addItem(reloadItem)
        viewMenuItem.submenu = viewMenu

        NSApp.mainMenu = mainMenu
    }

    @objc func reloadPage(_ sender: Any?) {
        webView?.reload()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()

        let rect = NSRect(x: 0, y: 0, width: 1280, height: 840)
        let win = NSWindow(
            contentRect: rect,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        win.title = "GameGuider"
        win.minSize = NSSize(width: 860, height: 560)
        win.appearance = NSAppearance(named: .darkAqua)
        win.backgroundColor = NSColor(calibratedRed: 0.043, green: 0.059, blue: 0.090, alpha: 1.0)
        win.center()

        let config = WKWebViewConfiguration()
        let wv = WKWebView(frame: rect, configuration: config)
        wv.navigationDelegate = self
        wv.setValue(false, forKey: "drawsBackground")
        win.contentView = wv
        self.window = win
        self.webView = wv

        if !smokeMode {
            win.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }

        startBackendServer()
    }

    func showMissingTools() {
        if smokeMode {
            fputs("SMOKE_NO_BACKEND: game-guide not found\n", stderr)
            exit(1)
        }
        let command = "__INSTALL_COMMAND__"
        let alert = NSAlert()
        alert.messageText = "GameGuider needs its command-line tools"
        alert.informativeText = "Run this in Terminal to install them, then open GameGuider again:\n\n\(command)"
        alert.addButton(withTitle: "Copy Command")
        alert.addButton(withTitle: "Quit")
        if alert.runModal() == .alertFirstButtonReturn {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(command, forType: .string)
        }
        NSApp.terminate(nil)
    }

    func startBackendServer() {
        let proc = Process()
        let exe = resolveExecutable()
        guard FileManager.default.isExecutableFile(atPath: exe) else {
            showMissingTools()
            return
        }
        proc.executableURL = URL(fileURLWithPath: exe)
        proc.arguments = ["web", "--no-open", "--port", "0"]

        var env = ProcessInfo.processInfo.environment
        let home = self.home()
        let extraPaths = [
            venvBinDir,
            "\(home)/.local/bin",
            "\(home)/.local/share/mise/shims",
            "/opt/homebrew/bin",
            "/usr/local/bin"
        ]
        let existingPath = env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        env["PATH"] = (extraPaths + [existingPath]).joined(separator: ":")
        env["PYTHONUNBUFFERED"] = "1"
        proc.environment = env

        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = FileHandle.standardError

        let handle = pipe.fileHandleForReading
        var buffer = ""
        handle.readabilityHandler = { [weak self] fh in
            let data = fh.availableData
            if data.isEmpty {
                fh.readabilityHandler = nil
                return
            }
            if let chunk = String(data: data, encoding: .utf8) {
                buffer += chunk
                if let range = buffer.range(of: #"http://127\.0\.0\.1:\d+"#, options: .regularExpression) {
                    let foundURL = String(buffer[range])
                    fh.readabilityHandler = nil
                    DispatchQueue.main.async {
                        self?.openServerURL(foundURL)
                    }
                }
            }
        }

        do {
            try proc.run()
            self.serverProcess = proc
        } catch {
            fputs("Failed to launch game-guide backend: \(error)\n", stderr)
            if smokeMode {
                exit(1)
            }
        }
    }

    func openServerURL(_ urlString: String) {
        self.loadedURL = urlString
        if let url = URL(string: urlString) {
            webView?.load(URLRequest(url: url))
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if smokeMode && !loadedURL.isEmpty {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in
                guard let self = self else { return }
                webView.evaluateJavaScript("typeof window.hydrate === 'function' && typeof window.playInlineVideo === 'function'") { result, error in
                    let ok = (result as? Bool) ?? false
                    if ok && error == nil {
                        compileContentRules { list, error in
                            if list == nil {
                                fputs("SMOKE_RULES_FAIL: \(String(describing: error))\n", stderr)
                                self.stopBackendServer()
                                exit(1)
                            }
                            print("SMOKE_OK \(self.loadedURL)")
                            fflush(stdout)
                            self.stopBackendServer()
                            exit(0)
                        }
                    } else {
                        fputs("SMOKE_JS_FAIL: \(String(describing: error))\n", stderr)
                        self.stopBackendServer()
                        exit(1)
                    }
                }
            }
        }
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        if navigationAction.shouldPerformDownload {
            decisionHandler(.download)
            return
        }
        // The app's own pages and its frames stay here; a clicked link to anywhere else leaves this view.
        guard let url = navigationAction.request.url, navigationAction.targetFrame?.isMainFrame ?? true,
              url.host != "127.0.0.1" && url.host != "localhost" && url.scheme != "about" else {
            decisionHandler(.allow)
            return
        }
        let web = ["http", "https"].contains(url.scheme?.lowercased() ?? "")
        if web && !navigationAction.modifierFlags.contains(.command) {  // ⌘-click: the system browser, as before
            if browser == nil { browser = BrowserWindow() }
            browser?.open(url)
        } else {
            NSWorkspace.shared.open(url)
        }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = suggestedFilename
        panel.begin { result in completionHandler(result == .OK ? panel.url : nil) }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        let alert = NSAlert()
        alert.messageText = "Could not save guide"
        alert.informativeText = "Try again with a different file name or location."
        alert.alertStyle = .warning
        alert.runModal()
    }

    func stopBackendServer() {
        if let proc = serverProcess, proc.isRunning {
            proc.terminate()
            proc.waitUntilExit()
        }
        serverProcess = nil
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopBackendServer()
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate(defaultBin: "__DEFAULT_BIN__", venvBinDir: "__VENV_BIN_DIR__")
app.delegate = delegate
app.run()
"""


def build_icon(icon_path: Path | None = None) -> Path:
    target = (icon_path or (PLUGIN_ROOT / "build" / "icon.icns")).resolve()
    if not target.is_file():
        icon_script = PLUGIN_ROOT / "build" / "icon.py"
        subprocess.run([sys.executable, str(icon_script)], check=True)
    if not target.is_file():
        raise FileNotFoundError(f"Icon was not generated at {target}")
    return target


def package_app(out_dir: Path | None = None) -> Path:
    base_out = (out_dir or (Path.cwd() / "out")).resolve()
    app_bundle = base_out if base_out.suffix == ".app" else (base_out / f"{APP_NAME}.app")
    contents = app_bundle / "Contents"
    macos_dir = contents / "MacOS"
    resources_dir = contents / "Resources"

    if app_bundle.exists():
        shutil.rmtree(app_bundle)
    macos_dir.mkdir(parents=True, exist_ok=True)
    resources_dir.mkdir(parents=True, exist_ok=True)

    icon_src = build_icon()
    shutil.copy2(icon_src, resources_dir / "icon.icns")

    info_plist = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": importlib.metadata.version("game-guide"),
        "CFBundleShortVersionString": importlib.metadata.version("game-guide"),
        "CFBundleExecutable": APP_NAME,
        "CFBundleIconFile": "icon.icns",
        "CFBundlePackageType": "APPL",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
    }
    with (contents / "Info.plist").open("wb") as f:
        plistlib.dump(info_plist, f)

    venv_bin_dir = Path(sys.executable).absolute().parent
    default_bin = venv_bin_dir / "game-guide"
    if not default_bin.exists():
        candidate = PLUGIN_ROOT / ".venv" / "bin" / "game-guide"
        if candidate.exists():
            default_bin = candidate
            venv_bin_dir = candidate.parent

    swift_source = (
        SWIFT_LAUNCHER
        .replace("__DEFAULT_BIN__", str(default_bin))
        .replace("__VENV_BIN_DIR__", str(venv_bin_dir))
        .replace("__CONTENT_RULES__", content_rules())
        .replace("__INSTALL_COMMAND__", INSTALL_COMMAND)
    )
    executable_path = macos_dir / APP_NAME
    with tempfile.TemporaryDirectory() as tmp:
        swift_file = Path(tmp) / "main.swift"
        swift_file.write_text(swift_source, encoding="utf-8")
        slices = []
        for arch in ("arm64", "x86_64"):  # universal, so one download runs on Apple silicon and Intel Macs
            slices.append(str(Path(tmp) / arch))
            subprocess.run(["/usr/bin/swiftc", "-O", "-target", f"{arch}-apple-macos13.0", str(swift_file),
                            "-framework", "AppKit", "-framework", "WebKit", "-o", slices[-1]], check=True)
        subprocess.run(["/usr/bin/lipo", "-create", *slices, "-output", str(executable_path)], check=True)
    executable_path.chmod(0o755)

    subprocess.run(
        ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app_bundle)],
        check=True,
    )
    return app_bundle


def _normalize_dock_url(url_str: str) -> str:
    parsed = urllib.parse.urlparse(url_str)
    if parsed.scheme == "file":
        return urllib.parse.unquote(parsed.path).rstrip("/")
    return url_str.rstrip("/")


def add_app_to_dock(app_path: Path, dock_plist: Path | None = None, restart_dock: bool = True) -> bool:
    app_resolved = app_path.resolve()
    app_uri = f"{app_resolved.as_uri().rstrip('/')}/"
    target_norm = _normalize_dock_url(app_uri)

    plist_file = (dock_plist or (Path.home() / "Library" / "Preferences" / "com.apple.dock.plist")).expanduser()
    if not plist_file.exists():
        return False

    data = plistlib.loads(plist_file.read_bytes())
    persistent_apps = data.setdefault("persistent-apps", [])

    for entry in persistent_apps:
        tile_data = entry.get("tile-data") or {}
        file_data = tile_data.get("file-data") or {}
        existing_url = file_data.get("_CFURLString") or ""
        if _normalize_dock_url(existing_url) == target_norm or tile_data.get("file-label") == app_resolved.stem:
            return False

    persistent_apps.append({
        "tile-type": "file-tile",
        "tile-data": {
            "file-label": app_resolved.stem,
            "file-data": {
                "_CFURLString": app_uri,
                "_CFURLStringType": 15,
            },
        },
    })

    raw_plist = plistlib.dumps(data, fmt=plistlib.FMT_BINARY)
    if dock_plist is not None:
        plist_file.write_bytes(raw_plist)
    else:
        subprocess.run(
            ["/usr/bin/defaults", "import", "com.apple.dock", "-"],
            input=raw_plist,
            check=True,
        )
    if restart_dock:
        subprocess.run(["/usr/bin/killall", "Dock"], check=False)
    return True


def install_app(dest_dir: Path = Path("/Applications"), add_to_dock: bool = True, app: Path | None = None) -> Path:
    """Install `app` (a prebuilt GameGuider.app, as the installer downloads), or build one, and pin it to the Dock."""
    built_app = app or package_app()
    target_dir = dest_dir.expanduser()
    if not os.access(target_dir, os.W_OK):
        target_dir = (Path.home() / "Applications").resolve()
        target_dir.mkdir(parents=True, exist_ok=True)

    installed_path = (target_dir / f"{APP_NAME}.app").resolve()
    if installed_path.exists():
        shutil.rmtree(installed_path)
    subprocess.run(["/usr/bin/ditto", str(built_app), str(installed_path)], check=True)
    subprocess.run(
        ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(installed_path)],
        check=True,
    )

    if add_to_dock:
        add_app_to_dock(installed_path)
    return installed_path
