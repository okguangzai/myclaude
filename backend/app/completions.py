"""Offline shell completion scripts derived from the actual Python parser."""


def generate(parser, shell, command):
    flags = sorted({flag for action in parser._actions for flag in action.option_strings})
    words = " ".join(flags)
    if shell == "bash":
        return f"complete -W '{words}' {command}"
    if shell == "zsh":
        return f"#compdef {command}\n_arguments '*:options:({words})'"
    if shell == "fish":
        return "\n".join(
            f"complete -c {command} -l {flag[2:]}" for flag in flags if flag.startswith("--")
        )
    if shell in {"powershell", "pwsh"}:
        choices = ", ".join("'" + flag + "'" for flag in flags)
        return f"Register-ArgumentCompleter -Native -CommandName {command} -ScriptBlock {{ param($wordToComplete, $commandAst, $cursorPosition) @({choices}) | Where-Object {{ $_ -like ($wordToComplete + '*') }} | ForEach-Object {{ [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterName', $_) }} }}"
    raise ValueError("shell must be bash, zsh, fish or powershell")
