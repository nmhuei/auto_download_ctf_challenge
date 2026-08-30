# -*- shell-script -*-
# bash completion cho `ctf` (ctf-toolkit) — sinh theo argparse của
# ctf_downloader/cli.py: subcommands + flag chính mỗi lệnh.
# Cài đặt:  source completions/ctf.bash   (hoặc copy vào ~/.local/share/bash-completion/completions/ctf)

_ctf() {
    local cur prev cword
    _init_completion || return

    local GLOBAL_OPTS="-v --version -i --interactive -w --workspace"

    local SUBCOMMANDS="pull download clone status tree ls dashboard note ghi-chu tag tags workspaces scan instance container spawn submit flag hoard flag-stash rank scoreboard leaderboard watch register reg doctor health checkup menu ui console storage du archive sync resync history log open sniper serve web git config"

    # Tìm subcommand cuối (bỏ qua option và giá trị của nó)
    local cmd=""
    for ((i = 1; i < cword; i++)); do
        case "${COMP_WORDS[i]}" in
            pull|download|clone)          cmd="pull" ;;
            status|tree|ls|dashboard)     cmd="status" ;;
            note|ghi-chu)                 cmd="note" ;;
            tag|tags)                     cmd="tag" ;;
            workspaces|scan)              cmd="workspaces" ;;
            instance|container|spawn)     cmd="instance" ;;
            submit|flag)                  cmd="submit" ;;
            hoard|flag-stash)             cmd="hoard" ;;
            rank|scoreboard|leaderboard)  cmd="rank" ;;
            watch)                        cmd="watch" ;;
            register|reg)                 cmd="register" ;;
            doctor|health|checkup)        cmd="doctor" ;;
            menu|ui|console)              cmd="menu" ;;
            storage|du|archive)           cmd="storage" ;;
            sync|resync)                  cmd="sync" ;;
            history|log)                  cmd="history" ;;
            open)                         cmd="open" ;;
            git)                          cmd="git" ;;
            config)                       cmd="config" ;;
            sniper)                       cmd="sniper" ;;
            serve|web)                    cmd="serve" ;;
        esac
    done

    local opts=""
    case "$cmd" in
        pull)        opts="-u --url -c --cookie -t --token -o --output -j --threads -C --category -E --exclude --no-third-party --no-template -f --force --verify-downloads --allow-private-redirects --update --refresh-meta --timeout --no-git --git-base --git-remote --no-git-push -i --interactive" ;;
        status)      opts="-w --workspace -u --unsolved -s --solved -C --category --container --label --search" ;;
        note)        opts="-w --workspace --remove" ;;
        tag)         opts="-r --remove -w --workspace" ;;
        workspaces)  opts="-d --dir" ;;
        instance)    opts="-w --workspace -c --cookie -t --token --id -n --name -l --list -i --interactive --auto-extend --auto-extend-all -y --yes" ;;
        submit)      opts="-w --workspace -u --url -c --cookie -t --token --id -n --name -f --flag --auto --flag-format --force -i --interactive" ;;
        hoard)       opts="-w --workspace --id -n --name -f --flag --list --all --remove" ;;
        rank)        opts="-w --workspace -u --url -c --cookie -t --token -n --top --no-docs" ;;
        watch)       opts="-w --workspace --once --no-scoreboard --start --end -c --cookie -t --token" ;;
        register)    opts="-u --url --email --tempmail --username --password --cf-clearance -w --workspace" ;;
        doctor)      opts="-u --url -w --workspace -c --cookie -t --token" ;;
        menu)        opts="-w --workspace -c --cookie -t --token" ;;
        storage)     opts="-d --base-dir --threshold-mb archive" ;;
        sync)        opts="-w --workspace --verify" ;;
        history)     opts="-w --workspace --all --tail --limit --prune --clear" ;;
        open)        opts="-w --workspace" ;;
        git)         opts="init status push finish end merge -d --dir -w --workspace --remote-url --remote --base --no-push --import-existing --keep-remote -m --message" ;;
        config)      opts="key value on off auto-sync workspace-root" ;;
        sniper)      opts="-w --workspace --start-at --retry-wrong --poll" ;;
        serve)       opts="-w --workspace --port" ;;
        *)           opts="$GLOBAL_OPTS $SUBCOMMANDS" ;;
    esac

    # `storage archive` — cấp con thứ hai
    if [[ "$cmd" == "storage" && " ${COMP_WORDS[*]} " == *" archive "* ]]; then
        opts="workspace_name --git-remote --out -y --yes"
    fi

    COMPREPLY=($(compgen -W "$opts" -- "$cur"))
}

complete -F _ctf ctf
