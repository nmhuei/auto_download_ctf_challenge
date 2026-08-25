#compdef ctf
# -*- shell-script -*-
# zsh completion cho `ctf` (ctf-toolkit) — sinh theo argparse của
# ctf_downloader/cli.py: subcommands + flag chính mỗi lệnh.
# Cài đặt: fpath=(<repo>/completions $fpath); autoload -Uz compinit && compinit

_ctf() {
    local -a global_opts=(
        '(-v --version)'{-v,--version}'[show version]'
        '(-i --interactive)'{-i,--interactive}'[launch full interactive CTF console]'
        '(-w --workspace)'{-w,--workspace}'[CTF workspace directory]:dir:_directories'
    )

    _arguments -C \
        $global_opts \
        '1:cmd:->cmds' \
        '*::arg:->args'

    case $state in
        cmds)
            local -a cmds=(
                'pull:Download challenges, files & build workspace'
                'status:Display challenge structure, points, and solve progress'
                'note:Ghi/xoá note cho một challenge'
                'tag:Thêm/xoá label cho một challenge'
                'workspaces:Scan and list all local CTF workspaces'
                'instance:Manage dynamic container instances from terminal'
                'submit:Submit flag to CTF platform'
                'hoard:Lưu flag vào kho local (không submit)'
                'rank:Display live scoreboard standings'
                'watch:Auto-sync trong event window của giải'
                'register:Tự tạo tài khoản trên platform'
                'doctor:Health-check platform trước giờ giải'
                'menu:Launch full interactive CTF suite dashboard'
                'storage:Kiểm soát dung lượng workspace + archive'
                'sync:Đồng bộ metadata workspace ↔ platform'
                'export-pack:Đóng gói writeup đã solve thành pack zip'
                'history:Lịch sử submit flag của workspace'
                'open:Mở thư mục challenge trong file manager/terminal'
                'sniper:Nộp flag tự động đúng giờ G'
                'serve:Dashboard web read-only cho workspace'
            )
            # alias map về lệnh chuẩn
            local -a aliases=(
                'download:alias of pull' 'clone:alias of pull'
                'tree:alias of status' 'ls:alias of status' 'dashboard:alias of status'
                'ghi-chu:alias of note' 'tags:alias of tag'
                'scan:alias of workspaces'
                'container:alias of instance' 'spawn:alias of instance'
                'flag:alias of submit' 'flag-stash:alias of hoard'
                'scoreboard:alias of rank' 'leaderboard:alias of rank'
                'reg:alias of register' 'health:alias of doctor' 'checkup:alias of doctor'
                'ui:alias of menu' 'console:alias of menu'
                'du:alias of storage' 'archive:alias of storage'
                'resync:alias of sync'
                'log:alias of history'
                'web:alias of serve'
            )
            _describe -t commands 'command' cmds && return 0
            _describe -t commands 'alias' aliases && return 0
            ;;
        args)
            case $words[1] in
                pull|download|clone)
                    _arguments \
                        '(-u --url)'{-u,--url}'[target CTF platform URL]:url' \
                        '(-c --cookie)'{-c,--cookie}'[cookie string or path to cookie file]:cookie:_files' \
                        '(-t --token)'{-t,--token}'[API token or Bearer token]:token' \
                        '(-o --output)'{-o,--output}'[output directory path]:dir:_directories' \
                        '(-j --threads)'{-j,--threads}'[number of download threads]:threads:' \
                        '(-C --category)'{-C,--category}'[only download specific categories]:category:' \
                        '(-E --exclude)'{-E,--exclude}'[exclude specific categories]:category:' \
                        '--no-third-party[disable downloading 3rd party links]' \
                        '--no-template[disable generating solve.py templates]' \
                        '(-f --force)'{-f,--force}'[force re-download existing files]' \
                        '--update[pull tăng dần: chỉ tải challenge mới + cập nhật metadata]' \
                        '--refresh-meta[như --update nhưng tải lại attachment khi thiếu]' \
                        '--timeout[request timeout in seconds]:seconds:' \
                        '(-i --interactive)'{-i,--interactive}'[launch interactive download wizard]'
                    ;;
                status|tree|ls|dashboard)
                    _arguments \
                        '(-w --workspace)'{-w,--workspace}'[CTF workspace directory]:dir:_directories' \
                        '(-u --unsolved)'{-u,--unsolved}'[show only unsolved challenges]' \
                        '(-s --solved)'{-s,--solved}'[show only solved challenges]' \
                        '(-C --category)'{-C,--category}'[filter specific categories]:category:' \
                        '--container[filter only dynamic container challenges]' \
                        '--label[chỉ hiện challenge mang label này]:label:' \
                        '--search[tìm từ khoá trong tên + note]:keyword:'
                    ;;
                note|ghi-chu)
                    _arguments \
                        ':target:(challenge id/name)' \
                        '*:content:' \
                        '(-w --workspace)'{-w,--workspace}'[CTF workspace directory]:dir:_directories' \
                        '--remove[xoá note của challenge]'
                    ;;
                tag|tags)
                    _arguments \
                        ':target:(challenge id/name)' \
                        '*:tag:' \
                        '(-r --remove)'{-r,--remove}'[xoá các tag khỏi challenge]' \
                        '(-w --workspace)'{-w,--workspace}'[CTF workspace directory]:dir:_directories'
                    ;;
                workspaces|scan)
                    _arguments \
                        '(-d --dir)'{-d,--dir}'[base CTF directory to scan]:dir:_directories'
                    ;;
                instance|container|spawn)
                    _arguments \
                        ':action:(start stop extend status list)' \
                        '(-w --workspace)'{-w,--workspace}'[CTF workspace directory]:dir:_directories' \
                        '(-c --cookie)'{-c,--cookie}'[cookie string or path]:cookie:_files' \
                        '(-t --token)'{-t,--token}'[API token or Bearer token]:token' \
                        '--id[target challenge ID]:id:' \
                        '(-n --name)'{-n,--name}'[target challenge name]:name:' \
                        '(-l --list)'{-l,--list}'[list all container challenges]' \
                        '(-i --interactive)'{-i,--interactive}'[interactive container wizard]' \
                        '--auto-extend[giữ sống container được chọn]' \
                        '--auto-extend-all[giữ sống mọi container running]' \
                        '(-y --yes)'{-y,--yes}'[xác nhận tự động]'
                    ;;
                submit|flag)
                    _arguments \
                        ':target:(challenge id/name)' \
                        ':flag_val:' \
                        '(-w --workspace)'{-w,--workspace}'[CTF workspace directory]:dir:_directories' \
                        '(-u --url)'{-u,--url}'[platform URL]:url' \
                        '(-c --cookie)'{-c,--cookie}'[cookie string or path]:cookie:_files' \
                        '(-t --token)'{-t,--token}'[API token or Bearer token]:token' \
                        '--id[target challenge ID]:id:' \
                        '(-n --name)'{-n,--name}'[target challenge name]:name:' \
                        '(-f --flag)'{-f,--flag}'[flag string to submit]:flag:' \
                        '--auto[auto-scan workspace for filled flags]' \
                        '--flag-format[regex định dạng flag]:regex:' \
                        '--force[vượt blacklist flag sai]' \
                        '(-i --interactive)'{-i,--interactive}'[interactive submission wizard]'
                    ;;
                hoard|flag-stash)
                    _arguments \
                        ':target:(challenge id/name)' \
                        ':flag_val:' \
                        '(-w --workspace)'{-w,--workspace}'[CTF workspace directory]:dir:_directories' \
                        '--id[target challenge ID]:id:' \
                        '(-n --name)'{-n,--name}'[target challenge name]:name:' \
                        '(-f --flag)'{-f,--flag}'[flag string to hoard]:flag:'
                    ;;
                rank|scoreboard|leaderboard)
                    _arguments \
                        '(-w --workspace)'{-w,--workspace}'[CTF workspace directory]:dir:_directories' \
                        '(-u --url)'{-u,--url}'[platform base URL]:url' \
                        '(-c --cookie)'{-c,--cookie}'[cookie string or path]:cookie:_files' \
                        '(-t --token)'{-t,--token}'[API token or Bearer token]:token' \
                        '(-n --top)'{-n,--top}'[number of top teams to display]:top:' \
                        '--no-docs[không ghi RANKING.md / SUMMARY.md]'
                    ;;
                watch)
                    _arguments \
                        '(-w --workspace)'{-w,--workspace}'[CTF workspace directory]:dir:_directories' \
                        '--once[chạy đúng 1 vòng rồi exit]' \
                        '--no-scoreboard[tắt tick scoreboard]' \
                        '--start[bắt đầu giải (ISO-8601 hoặc epoch)]:time:' \
                        '--end[kết thúc giải (ISO-8601 hoặc epoch)]:time:' \
                        '(-c --cookie)'{-c,--cookie}'[cookie string or path]:cookie:_files' \
                        '(-t --token)'{-t,--token}'[API token or Bearer token]:token'
                    ;;
                register|reg)
                    _arguments \
                        '(-u --url)'{-u,--url}'[URL platform]:url' \
                        '--email[email dùng để đăng ký]:email:' \
                        '--tempmail[dùng mailbox tạm mail.tm]' \
                        '--username[prefix username]:prefix:' \
                        '--password[mật khẩu muốn đặt]:password:' \
                        '(-w --workspace)'{-w,--workspace}'[workspace gắn credentials]:dir:_directories'
                    ;;
                doctor|health|checkup)
                    _arguments \
                        '(-u --url)'{-u,--url}'[platform URL]:url' \
                        '(-w --workspace)'{-w,--workspace}'[workspace lấy auth từ auth map]:dir:_directories' \
                        '(-c --cookie)'{-c,--cookie}'[cookie string or path]:cookie:_files' \
                        '(-t --token)'{-t,--token}'[API token or Bearer token]:token'
                    ;;
                menu|ui|console)
                    _arguments \
                        '(-w --workspace)'{-w,--workspace}'[CTF workspace directory]:dir:_directories' \
                        '(-c --cookie)'{-c,--cookie}'[cookie string or path]:cookie:_files' \
                        '(-t --token)'{-t,--token}'[API token or Bearer token]:token'
                    ;;
                storage|du)
                    if (( $words[(I)archive] )); then
                        _arguments \
                            ':workspace_name:' \
                            '--git-remote[git remote URL để commit + push]:remote:' \
                            '--out[thư mục lưu archive]:dir:_directories' \
                            '(-y --yes)'{-y,--yes}'[bỏ qua confirm archive]'
                    else
                        _arguments \
                            '(-d --base-dir)'{-d,--base-dir}'[thư mục gốc chứa workspace]:dir:_directories' \
                            '--threshold-mb[ngưỡng cảnh báo dung lượng MiB]:mb:' \
                            '1:subcmd:(archive)'
                    fi
                    ;;
                sync|resync)
                    _arguments \
                        '(-w --workspace)'{-w,--workspace}'[CTF workspace directory]:dir:_directories' \
                        '--verify[chạy thêm verify drift solved server/local]'
                    ;;
                export-pack)
                    _arguments \
                        '(-w --workspace)'{-w,--workspace}'[CTF workspace directory]:dir:_directories' \
                        '--out[thư mục lưu pack zip]:dir:_directories'
                    ;;
                history|log)
                    _arguments \
                        '(-w --workspace)'{-w,--workspace}'[CTF workspace directory]:dir:_directories' \
                        '--all[hiện flag đầy đủ]'
                    ;;
                sniper)
                    _arguments \
                        '(-w --workspace)'{-w,--workspace}'[CTF workspace directory]:dir:_directories' \
                        '--start-at[thời điểm mở giải ISO-8601/epoch]:time:' \
                        '--retry-wrong[cho phép thử lại target sai]' \
                        '--poll[chu kỳ poll khi chờ giờ G (giây)]:seconds:'
                    ;;
                open)
                    _arguments \
                        ':target:(challenge id/name)' \
                        '(-w --workspace)'{-w,--workspace}'[CTF workspace directory]:dir:_directories'
                    ;;
                serve|web)
                    _arguments \
                        '(-w --workspace)'{-w,--workspace}'[CTF workspace directory]:dir:_directories' \
                        '--port[port HTTP]:port:'
                    ;;
                *)
                    _arguments $global_opts
                    ;;
            esac
            ;;
    esac
}

_ctf "$@"
