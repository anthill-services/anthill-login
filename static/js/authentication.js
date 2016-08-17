
function def()
{
    return $.Deferred();
}

OPTS = {};

function auth_with(social_name)
{
    var d = def();

    var social = SOCIAL[social_name];

    social.auth().done(function(social, data)
    {
        authenticate(social, data).done(function(token)
        {
            d.resolve(token)
        }).fail(function(reason, data, responseText)
        {
            d.reject(reason, data, responseText);
        });
    }).fail(function(reason, data, responseText)
    {
        d.reject(reason, data, responseText);
    });

    return d.promise();
}

function auth_init(location, options)
{
    SOCIAL = {
        facebook:
        {
            scopes: 'public_profile,user_friends',
            init: function(data)
            {
                this.scopes = data.scopes;

                var d = def();

                window.fbAsyncInit = function()
                {
                    FB.init({
                        appId: data.client_id,
                        cookie: true,
                        version: 'v2.5'
                    });
                };

                $.getScript("https://connect.facebook.net/en_US/sdk.js");

                return d.promise();
            },
            auth: function()
            {
                var d = def();

                FB.login(function(response)
                {
                    if (response.authResponse)
                    {
                        var access_token = response.authResponse.accessToken;
                        var user_id = response.authResponse.userID;

                        d.resolve("facebook", {
                            "username": user_id,
                            "key": access_token
                        });
                    }
                },
                {
                    scope: this.scopes
                });

                return d.promise();
            }
        },
        google:
        {
            scopes: 'https://www.googleapis.com/auth/plus.login',
            init: function(data)
            {
                this.scopes = data.scopes;

                var d = def();

                window.google_init = function()
                {
                    //noinspection JSUnresolvedVariable
                    gapi.load('auth2', function()
                    {
                        //noinspection JSUndeclaredVariable,JSUnresolvedVariable
                        auth2 = gapi.auth2.init({
                            client_id: data.client_id
                        });

                        d.resolve();
                    });
                };

                $.getScript("https://apis.google.com/js/client:platform.js?onload=google_init");

                return d.promise();
            },
            auth: function()
            {
                var d = def();

                //noinspection JSUnresolvedFunction
                auth2.grantOfflineAccess({'redirect_uri': 'postmessage', 'scope': this.scopes}).then(function(data)
                {
                    if (data != undefined)
                    {
                        var code = data["code"];

                        if (code != undefined)
                        {
                            d.resolve("google", {
                                "key": code
                            });
                        }
                    }
                });

                return d.promise();
            }
        },
        dev:
        {
            init: function(data)
            {
            },
            auth: function()
            {
                var d = def();

                window.devauth = function(username, password)
                {
                    d.resolve("dev", {
                        "username": username,
                        "key": password
                    })
                };

                window.popup(OPTS.location + "/authdev?callback=devauth", "Authenticate", 360, 360);

                return d.promise();
            }
        }
    };

    var sns = options["sns"];

    for (var sns_id in sns)
    {
        var sns_data = sns[sns_id];
        var social = SOCIAL[sns_id];

        social.init(sns_data);
    }

    OPTS["location"] = location;
    OPTS["gamespace"] = options.gamespace;
    OPTS["scopes"] = options.scopes || "";
    OPTS["should_have"] = options.should_have || "*";
    OPTS["attach_to"] = options.attach_to || "";
    OPTS["auth_as"] = options.auth_as || "";
}

function resolve_conflict(method, resolve_with, resolve_token)
{
    var d = def();

    var params = {
        "resolve_with": resolve_with,
        "resolve_method": method,
        "access_token": resolve_token,
        "scopes": OPTS.scopes,
        "full": true,
        "should_have": OPTS.should_have,
        "attach_to": OPTS["attach_to"]
    };

    $.post(OPTS.location + "/resolve", params, function(data, textStatus)
    {
        if (textStatus == "success")
        {
            var token = data["token"];

            d.resolve(token);
        }
    }, "json").fail(function(reason, text, statusText)
    {
        d.reject(statusText, {}, reason.responseText);
    });

    return d.promise();
}

function auth_redirect(to, data)
{
    $.redirect(to, data, "POST", "");
}

function render_account(parent, account)
{
    var profile_avatar = account.profile["avatar"];
    var profile_name = account.profile["name"];

    var time_created = account.profile["@time_created"];
    var time_updated = account.profile["@time_updated"];

    if (profile_avatar != null || profile_name != null)
    {
        var node = $('<div></div>').appendTo(parent);

        if (profile_avatar != null)
        {
            $('<img class="img-circle" width="100px" src="' + profile_avatar + ' ">').appendTo(node);
        }
        if (profile_name != null)
        {
            $('<h3>' + profile_name + '</h3>').appendTo(node);
        }
    }
    else
    {
        if ($.isEmptyObject(account.profile))
        {
            parent.append('<p>( No profile )</p>');
        }
        else
        {
            var profile_text = JSON.stringify(account.profile, null, 4);
            parent.append('<p><code>' + profile_text + '</code></p>');
        }
    }

    function time(time)
    {
        var t = new Date(time * 1000);
        return t.toDateString() + ' ' + t.toTimeString();
    }

    if (time_created != null)
    {
        parent.append('<p>Created: ' + time(time_created) + '</p>');
    }

    if (time_updated != null)
    {
        parent.append('<p>Last updated: ' + time(time_updated) + '</p>');
    }
}

function conflict(response)
{
    var d = def();

    var reason = response["result_id"];
    var resolve_token = response["resolve_token"];

    if (reason == "merge_required")
    {
        // well, that's merge required
        var accounts = response["accounts"];

        var local = accounts["local"];
        var remote = accounts["remote"];

        render_account($('#account-ismine'), remote);

        $('#form-ismine').modal();

        $('#ismine-yes').on("click", function()
        {
            $('#form-ismine').modal('hide');
            $('#conflict-choose').modal();

            var account_choose = $('#account-choose');
            var row = $('<div align="center"></div>').appendTo(account_choose);

            for (var account in accounts)
            {
                var resolve = account;
                var data = accounts[account];

                var select = $('<div class="col-sm-6" style="text-align: center;"></div>').appendTo(row);

                var button = $('<p><a href="#" class="btn btn-danger" role="button">Use account @' +
                    data["account"] + '</a></p><hr>').appendTo(select);

                (function(resolve)
                {
                    button.click(function()
                    {
                        $('#conflict-choose').modal('hide');
                        $('#conflict-progress').modal();

                        resolve_conflict("merge_required", resolve, resolve_token).done(function(token)
                        {
                            d.resolve(token);
                        }).fail(function(reason, text, statusText)
                        {
                            d.reject(reason, text, statusText);
                        });
                    });
                })(resolve);


                render_account(select, data);
            }
        });

        $('#ismine-no').one("click", function()
        {
            $('#conflict-ismine').modal('hide');
            $('#conflict-progress').modal();

            resolve_conflict("merge_required", "not_mine", resolve_token).done(function(token)
            {
                d.resolve(token);
            }).fail(function(reason, text, statusText)
            {
                d.reject(reason, text, statusText);
            });
        });
    } else
    if (reason == "multiple_accounts_attached")
    {
        var accounts = response["accounts"];

        $('#conflict-choose').modal();

        var account_choose = $('#account-choose').html('');
        var row = $('<div align="center"></div>').appendTo(account_choose);

        for (var account in accounts)
        {
            var resolve = account;
            var data = accounts[account];

            var select = $('<div class="col-sm-6" style="text-align: center;"></div>').appendTo(row);

            var button = $('<p><a href="#" class="btn btn-danger" role="button">Use account @' +
                data["account"] + '</a></p><hr>').appendTo(select);

            (function(data)
            {
                button.click(function()
                {
                    $('#conflict-choose').modal('hide');
                    $('#conflict-progress').modal();

                    resolve_conflict("multiple_accounts_attached", data["account"], resolve_token).done(function(token)
                    {
                        d.resolve(token);
                    }).fail(function(reason, text, statusText)
                    {
                        d.reject(reason, text, statusText);
                    });
                });
            })(data);


            render_account(select, data);
        }
    }

    return d.promise();
}

function authenticate(credential, data)
{
    var d = def();

    var params = {
        "credential": credential,
        "gamespace": OPTS["gamespace"],
        "scopes": OPTS["scopes"],
        "as": OPTS["auth_as"],
        "full": true,
        "should_have": OPTS["should_have"]
    };

    if (OPTS["attach_to"] != "")
    {
        params["attach_to"] = OPTS["attach_to"];
    }

    $.extend(params, data);
    $.post(OPTS.location + "/auth", params, function(data, textStatus)
    {
        if (textStatus == "success")
        {
            var token = data["token"];
            d.resolve(token);
        }
    }, "json").fail(function(data)
    {
        var status = data.status;

        var response = $.parseJSON(
            data.responseText
        );

        var result_id = response["result_id"];

        switch (status)
        {
            case 409:
            case 300:
            {
                var resolve_token = response["resolve_token"];

                $('#auth-root').load(OPTS.location + "/js/conflict.in.html", function()
                {
                    conflict(response).fail(function(reason, data, responseText)
                    {
                        d.reject(
                            reason,
                            data,
                            responseText);

                    }).done(function(token)
                    {
                        d.resolve(token);
                    });
                });

                break;
            }
            default:
            {
                d.reject(
                    "unknown",
                    response, data.responseText);

                break;
            }
        }
    });

    return d.promise();
}

function popup(url, title, w, h)
{
    var dualScreenLeft = window.screenLeft != undefined ? window.screenLeft : screen.left;
    var dualScreenTop = window.screenTop != undefined ? window.screenTop : screen.top;

    var width = window.innerWidth ? window.innerWidth : document.documentElement.clientWidth
        ? document.documentElement.clientWidth : screen.width;
    var height = window.innerHeight ? window.innerHeight : document.documentElement.clientHeight
        ? document.documentElement.clientHeight : screen.height;

    var left = ((width / 2) - (w / 2)) + dualScreenLeft;
    var top = ((height / 2) - (h / 2)) + dualScreenTop;
    var newWindow = window.open(url, title, 'scrollbars=no, width=' + w + ', height=' + h + ', top=' +
        top + ', left=' + left);

    if (window.focus && newWindow != undefined)
    {
        newWindow.focus();
    }
}