import Cocoa
import Foundation

// MARK: - 项目定位

let fm = FileManager.default

func embeddedProjectRoot() -> String? {
    return Bundle.main.object(forInfoDictionaryKey: "JarvisProjectRoot") as? String
}

/// App 放在项目根目录时，直接用 App 的上一级；被拖到别处时，退回打包时写入的路径或用户主目录标准路径。
func resolveProjectRoot() -> URL {
    let sibling = Bundle.main.bundleURL.deletingLastPathComponent()
    if fm.fileExists(atPath: sibling.appendingPathComponent("jarvis").path) {
        return sibling
    }
    if let path = embeddedProjectRoot(), !path.isEmpty {
        let url = URL(fileURLWithPath: path)
        if fm.fileExists(atPath: url.appendingPathComponent("jarvis").path) {
            return url
        }
    }
    let home = fm.homeDirectoryForCurrentUser
    let candidates = [
        home.appendingPathComponent("Jarvis-Gateway"),
        home.appendingPathComponent("jarvis-gateway"),
        home.appendingPathComponent("Developer/Jarvis-Gateway"),
        home.appendingPathComponent("Documents/Jarvis-Gateway")
    ]
    for candidate in candidates {
        if fm.fileExists(atPath: candidate.appendingPathComponent("jarvis").path) {
            return candidate
        }
    }
    return sibling
}

final class Box {
    var text = ""
}

// MARK: - 菜单栏 App

final class AppDelegate: NSObject, NSApplicationDelegate {
    let root = resolveProjectRoot()

    var statusItem: NSStatusItem!
    var menu: NSMenu!
    var statusHeader: NSMenuItem!
    var infoHeader: NSMenuItem!
    var startItem: NSMenuItem!
    var stopItem: NSMenuItem!
    var restartItem: NSMenuItem!
    var autostartItem: NSMenuItem!
    var launchAutoItem: NSMenuItem!

    var isRunning = false
    var isBusy = false
    var pollTimer: Timer?

    let defaults = UserDefaults.standard

    // MARK: 生命周期

    func applicationDidFinishLaunching(_ notification: Notification) {
        if defaults.object(forKey: "autoStartOnLaunch") == nil {
            defaults.set(true, forKey: "autoStartOnLaunch")
        }

        buildStatusItem()
        buildMenu()
        refresh()

        pollTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        if let timer = pollTimer {
            RunLoop.main.add(timer, forMode: .common)
        }

        if defaults.bool(forKey: "autoStartOnLaunch") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
                guard let self = self else { return }
                self.runCLI(["start", "--no-wait"], label: "启动服务", quiet: true)
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        pollTimer?.invalidate()
    }

    /// 已经运行时再次双击 App：直接把控制台打开，给个明确反馈
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openDashboard()
        return true
    }

    // MARK: 界面

    func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = statusImage(running: false)
            button.imagePosition = .imageOnly
            button.toolTip = "Jarvis：检测中…"
        }
    }

    func statusImage(running: Bool) -> NSImage? {
        let names = running
            ? ["waveform", "bolt.horizontal.fill", "circle.fill"]
            : ["waveform.slash", "bolt.horizontal", "circle"]
        for name in names {
            if let image = NSImage(systemSymbolName: name, accessibilityDescription: "Jarvis") {
                image.isTemplate = true
                return image
            }
        }
        return nil
    }

    func makeItem(_ title: String, _ action: Selector?, key: String = "") -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        return item
    }

    func buildMenu() {
        menu = NSMenu()
        menu.autoenablesItems = false

        statusHeader = makeItem("Jarvis 网关：检测中…", nil)
        statusHeader.isEnabled = false
        menu.addItem(statusHeader)

        infoHeader = makeItem("正在读取状态…", nil)
        infoHeader.isEnabled = false
        menu.addItem(infoHeader)

        menu.addItem(.separator())

        startItem = makeItem("启动服务", #selector(startServices))
        stopItem = makeItem("停止服务", #selector(stopServices))
        restartItem = makeItem("重启服务", #selector(restartServices))
        menu.addItem(startItem)
        menu.addItem(stopItem)
        menu.addItem(restartItem)

        menu.addItem(.separator())

        menu.addItem(makeItem("打开控制台", #selector(openDashboard), key: "o"))
        menu.addItem(makeItem("打开日志目录", #selector(openLogsFolder)))
        menu.addItem(makeItem("打开辅助功能设置", #selector(openAccessibilitySettings)))

        menu.addItem(.separator())

        autostartItem = makeItem("登录时自动启动服务", #selector(toggleAutostart))
        menu.addItem(autostartItem)

        launchAutoItem = makeItem("打开 App 时自动拉起服务", #selector(toggleLaunchAuto))
        menu.addItem(launchAutoItem)

        menu.addItem(.separator())

        menu.addItem(makeItem("退出（服务继续运行）", #selector(quitKeepRunning), key: "q"))
        menu.addItem(makeItem("退出并停止服务", #selector(quitAndStop)))

        statusItem.menu = menu
        updateUI()
    }

    func updateUI() {
        let autostartOn = fm.fileExists(atPath: autostartPlistURL().path)
        autostartItem.state = autostartOn ? .on : .off
        launchAutoItem.state = defaults.bool(forKey: "autoStartOnLaunch") ? .on : .off

        startItem.isEnabled = !isRunning && !isBusy
        stopItem.isEnabled = isRunning && !isBusy
        restartItem.isEnabled = isRunning && !isBusy

        statusItem.button?.image = statusImage(running: isRunning)
        statusItem.button?.toolTip = isRunning ? "Jarvis：运行中" : "Jarvis：已停止"
    }

    // MARK: 状态轮询

    func refresh() {
        guard let url = URL(string: "http://127.0.0.1:18888/status") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.5
        request.cachePolicy = .reloadIgnoringLocalCacheData

        URLSession.shared.dataTask(with: request) { [weak self] data, _, _ in
            var payload: [String: Any]? = nil
            if let data = data,
               let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
                payload = object
            }
            DispatchQueue.main.async {
                self?.apply(payload)
            }
        }.resume()
    }

    func apply(_ payload: [String: Any]?) {
        guard !isBusy else { return }

        if let gateway = payload?["gateway"] as? [String: Any],
           (gateway["online"] as? Bool) == true {
            isRunning = true
            let whisper = ((payload?["whisper"] as? [String: Any])?["online"] as? Bool) ?? false
            let dataOn = ((payload?["data"] as? [String: Any])?["online"] as? Bool) ?? false
            let mode = (gateway["mode"] as? String) ?? "-"
            let tokens = ((payload?["data"] as? [String: Any])?["tokens_str"] as? String) ?? "--"
            let turns = ((payload?["stats"] as? [String: Any])?["turns"] as? Int) ?? 0

            statusHeader.title = "网关运行中 · 识别\(whisper ? "就绪" : "预热中") · 数据\(dataOn ? "在线" : "离线")"
            infoHeader.title = "模式 \(mode) · 今日 \(tokens) · 本次 \(turns) 轮"
        } else {
            isRunning = false
            statusHeader.title = "网关未启动"
            infoHeader.title = "点「启动服务」，或双击项目里的启动脚本"
        }

        updateUI()
    }

    // MARK: 动作

    @objc func startServices() {
        runCLI(["start", "--no-wait"], label: "启动服务")
    }

    @objc func stopServices() {
        runCLI(["stop"], label: "停止服务")
    }

    @objc func restartServices() {
        runCLI(["restart"], label: "重启服务")
    }

    @objc func openDashboard() {
        if let url = URL(string: "http://127.0.0.1:18888/") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc func openLogsFolder() {
        NSWorkspace.shared.open(root.appendingPathComponent("logs"))
    }

    @objc func openAccessibilitySettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc func toggleAutostart() {
        let action = fm.fileExists(atPath: autostartPlistURL().path) ? "off" : "on"
        runCLI(["autostart", action], label: action == "on" ? "开启开机自启" : "关闭开机自启")
    }

    @objc func toggleLaunchAuto() {
        let next = !defaults.bool(forKey: "autoStartOnLaunch")
        defaults.set(next, forKey: "autoStartOnLaunch")
        updateUI()
    }

    @objc func quitKeepRunning() {
        NSApp.terminate(nil)
    }

    @objc func quitAndStop() {
        runCLI(["stop"], label: "停止服务") {
            NSApp.terminate(nil)
        }
    }

    // MARK: 执行 CLI

    func autostartPlistURL() -> URL {
        return fm.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/ai.jarvis.gateway.plist")
    }

    func runCLI(_ arguments: [String], label: String, quiet: Bool = false,
                completion: (() -> Void)? = nil) {
        if isBusy {
            NSSound.beep()
            return
        }
        isBusy = true
        statusHeader.title = "\(label)中…"
        updateUI()

        let python = root.appendingPathComponent("venv/bin/python")
        let script = root.appendingPathComponent("jarvis")

        guard fm.fileExists(atPath: python.path),
              fm.fileExists(atPath: script.path) else {
            isBusy = false
            updateUI()
            showAlert("找不到 Jarvis 工程",
                      "请确认 App 还在 Jarvis-Gateway 目录里：\n\(root.path)")
            return
        }

        let process = Process()
        process.executableURL = python
        process.arguments = [script.path] + arguments
        process.currentDirectoryURL = root

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        process.standardInput = FileHandle.nullDevice

        process.terminationHandler = { [weak self] proc in
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            let output = String(data: data, encoding: .utf8) ?? ""
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.isBusy = false
                if proc.terminationStatus != 0 && !quiet {
                    self.showAlert("\(label)失败",
                                   output.isEmpty ? "退出码 \(proc.terminationStatus)" : output)
                }
                self.refresh()
                completion?()
            }
        }

        do {
            try process.run()
        } catch {
            isBusy = false
            updateUI()
            if !quiet {
                showAlert("\(label)失败", error.localizedDescription)
            }
        }
    }

    func showAlert(_ title: String, _ text: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = text
        alert.alertStyle = .warning
        alert.addButton(withTitle: "好")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }
}

// MARK: - 自检（不启动界面）

if CommandLine.arguments.contains("--selftest") {
    let root = resolveProjectRoot()
    print("project_root=\(root.path)")
    print("jarvis=\(fm.fileExists(atPath: root.appendingPathComponent("jarvis").path))")
    print("venv=\(fm.fileExists(atPath: root.appendingPathComponent("venv/bin/python").path))")
    print("icon=\(fm.fileExists(atPath: Bundle.main.bundlePath + "/Contents/Resources/AppIcon.icns"))")

    let box = Box()
    let semaphore = DispatchSemaphore(value: 0)
    var request = URLRequest(url: URL(string: "http://127.0.0.1:18888/ping")!)
    request.timeoutInterval = 2
    URLSession.shared.dataTask(with: request) { data, _, _ in
        if let data = data, let text = String(data: data, encoding: .utf8) {
            box.text = text
        }
        semaphore.signal()
    }.resume()
    _ = semaphore.wait(timeout: .now() + 3)
    print("ping=\(box.text.isEmpty ? "no_response" : box.text)")
    exit(0)
}

// MARK: - 启动

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.accessory)
application.run()
