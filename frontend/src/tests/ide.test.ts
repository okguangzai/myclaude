import { it, expect, vi } from "vitest";
import vm from "node:vm";
import { readFileSync } from "node:fs";
import path from "node:path";
it("IDE extension configures secret storage, submits selection read-only and consumes SSE", async () => {
  const source = readFileSync(
    path.resolve(process.cwd(), "../backend/app/ide_extension.js"),
    "utf8",
  );
  const commands: Record<string, () => Promise<void>> = {},
    values = new Map(),
    secrets = new Map();
  const input = [
    "https://localhost:8010/",
    "fixture-user",
    "fixture-password",
    "Explain this selection",
  ];
  let output = "";
  const requests: any[] = [];
  const vscode = {
    window: {
      createOutputChannel: () => ({
        append: (s: string) => (output += s),
        appendLine: (s: string) => (output += s),
        clear: () => (output = ""),
        show: () => {},
      }),
      showInputBox: async () => input.shift(),
      activeTextEditor: {
        selection: {},
        document: { getText: () => "const x = 1;" },
      },
      withProgress: async (_: any, fn: any) =>
        fn({}, { onCancellationRequested: () => ({ dispose: () => {} }) }),
      showErrorMessage: vi.fn(),
    },
    commands: {
      registerCommand: (name: string, fn: any) => {
        commands[name] = fn;
        return { dispose() {} };
      },
    },
    ProgressLocation: { Notification: 1 },
  };
  const encoder = new TextEncoder();
  const fetch = vi.fn(async (url: string, init: any) => {
    requests.push({ url, ...init });
    if (url.endsWith("/sessions"))
      return { ok: true, json: async () => ({ id: "ide-session" }) };
    let done = false;
    return {
      ok: true,
      body: {
        getReader: () => ({
          read: async () =>
            done
              ? { done: true }
              : ((done = true),
                {
                  done: false,
                  value: encoder.encode(
                    'data: {"type":"delta","text":"explained"}\n\ndata: {"type":"run_end","status":"completed"}\n\n',
                  ),
                }),
        }),
      },
    };
  });
  const sandbox: any = {
    require: (name: string) => {
      if (name === "vscode") return vscode;
      throw Error(name);
    },
    module: { exports: {} },
    Buffer,
    URL,
    fetch,
    AbortController,
    TextDecoder,
  };
  vm.runInNewContext(source, sandbox);
  sandbox.module.exports.activate({
    subscriptions: [],
    globalState: {
      get: (k: string, d: any) => values.get(k) ?? d,
      update: async (k: string, v: any) => values.set(k, v),
    },
    secrets: {
      store: async (k: string, v: any) => secrets.set(k, v),
      get: async (k: string) => secrets.get(k),
    },
  });
  await commands["myclaude.configure"]();
  await commands["myclaude.ask"]();
  expect(vscode.window.showErrorMessage).not.toHaveBeenCalled();
  expect(values.has("authorization")).toBe(false);
  expect(secrets.get("authorization")).toMatch(/^Basic /);
  expect(requests[1].url).toBe("https://localhost:8010/api/chat");
  expect(JSON.parse(requests[1].body)).toEqual({
    session_id: "ide-session",
    permission: "read-only",
    message: "Explain this selection\n\nSelected code:\nconst x = 1;",
  });
  expect(output).toContain("explained");
  expect(output).toContain("[completed]");
});
