var oAttachmentItem;

var noLink = /^(?:H1|H2|H3|H4|H5|H6|PRE|TT|IMG)$/i;

if (1 || !FCKBrowserInfo.IsIE){

function LinkState()
{
  if (FCKSelection.CheckForNodeNames(noLink))
  {
    return FCK_TRISTATE_DISABLED;
  }
  return (FCK.GetNamedCommandState('CreateAttachment')==FCK_TRISTATE_ON) ? 
    FCK_TRISTATE_ON : FCK_TRISTATE_OFF;
}

// Register the related command.
FCKCommands.RegisterCommand('Attachment', new FCKDialogCommand( 'Attachment', FCKLang.DlgLnkWindowTitle, FCKConfig['WikiBasePath'] + '?action=fckdialog&dialog=attachment', 400, 330, LinkState, 'CreateAttachment')) ;

oAttachmentItem = new FCKToolbarButton('Attachment', FCKLang.AttachmentBtn, null, null, false, true);
} 
else
{
FCKCommands.RegisterCommand('Attachment', new FCKDialogCommand( 'Attachment', FCKLang.DlgLnkWindowTitle, FCKConfig['WikiBasePath'] + '?action=fckdialog&dialog=attachment', 400, 330, FCK.GetNamedCommandState, 'CreateAttachment')) ;

oAttachmentItem = new FCKToolbarButton('Attachment', FCKLang.AttachmentBtn, null, null, false, false);
}

// Create the "Attachment" toolbar button.
oAttachmentItem.IconPath = FCKPlugins.Items['moinattachment'].Path + 'attachment.gif';
FCKToolbarItems.RegisterItem('Attachment', oAttachmentItem);

