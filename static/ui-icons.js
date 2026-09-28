(function () {
    'use strict';
    const names = new Set(('folder folder-plus folder-open chevron-right chevron-left '
        + 'check plus pin pin-off bell bell-off archive archive-restore pencil trash '
        + 'copy reply forward message-circle messages users user settings search '
        + 'paperclip image video circle-video mic music file link globe clock send '
        + 'play pause stop close phone ban shield download smartphone sun moon home '
        + 'log-out menu more-vertical more-horizontal list check-square refresh '
        + 'alert-circle info smile sticker crown scissors crop rotate undo eye lock inbox').split(' '));
    window.synapseIcon = function (name, extraClass) {
        if (!names.has(name)) name = 'file';
        const classes = typeof extraClass === 'string'
            ? extraClass.split(/\s+/).filter(token => /^[a-zA-Z][a-zA-Z0-9_-]*$/.test(token))
            : [];
        return '<svg class="ui-icon' + (classes.length ? ' ' + classes.join(' ') : '')
            + '" aria-hidden="true" focusable="false"><use href="/static/ui-icons.svg#'
            + name + '"></use></svg>';
    };
})();
