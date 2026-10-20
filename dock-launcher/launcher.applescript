use AppleScript version "2.4"
use scripting additions

on run
    set dashboardUrl to "http://localhost:8791/"

    tell application "System Events"
        set chromeRunning to (exists (processes where name is "Google Chrome"))
    end tell

    if chromeRunning then
        try
            tell application "Google Chrome"
                set foundTab to false
                repeat with w in windows
                    set tabIdx to 0
                    repeat with t in tabs of w
                        set tabIdx to tabIdx + 1
                        set u to URL of t
                        if u is dashboardUrl or u is text 1 thru -2 of dashboardUrl then
                            set active tab index of w to tabIdx
                            set index of w to 1
                            set foundTab to true
                            exit repeat
                        end if
                    end repeat
                    if foundTab then exit repeat
                end repeat

                if not foundTab then
                    if (count of windows) is 0 then
                        open location dashboardUrl
                    else
                        tell window 1 to make new tab with properties {URL:dashboardUrl}
                    end if
                end if
                activate
            end tell
        on error
            do shell script "/usr/bin/open " & quoted form of dashboardUrl
        end try
    else
        do shell script "/usr/bin/open " & quoted form of dashboardUrl
    end if

    tell me to quit
end run
