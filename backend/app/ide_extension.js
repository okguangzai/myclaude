const vscode = require('vscode');
function activate(context) {
  const output = vscode.window.createOutputChannel('MyClaude');
  context.subscriptions.push(output);
  async function configure() {
    const url = await vscode.window.showInputBox({prompt:'MyClaude HTTPS URL',value:context.globalState.get('url','http://127.0.0.1:8000')});
    if (!url) return;
    const parsed = new URL(url);
    if (parsed.protocol !== 'https:' && !['localhost','127.0.0.1'].includes(parsed.hostname)) throw Error('Use HTTPS for remote servers');
    const user = await vscode.window.showInputBox({prompt:'HTTP Basic username (blank for local)'});
    if (user === undefined) return;
    const password = user ? await vscode.window.showInputBox({prompt:'HTTP Basic password',password:true}) : '';
    if (password === undefined) return;
    await context.globalState.update('url',url.replace(/\/$/,''));
    await context.secrets.store('authorization',user ? 'Basic '+Buffer.from(user+':'+password).toString('base64') : '');
  }
  async function ask() {
    if (!context.globalState.get('url')) await configure();
    const url = context.globalState.get('url'); if (!url) return;
    const editor = vscode.window.activeTextEditor;
    const selection = editor ? editor.document.getText(editor.selection) : '';
    const question = await vscode.window.showInputBox({prompt:selection ? 'Question (selected text will be sent to your configured server)' : 'Question for MyClaude'});
    if (!question) return;
    const authorization = await context.secrets.get('authorization');
    const headers = {'Content-Type':'application/json',...(authorization ? {Authorization:authorization} : {})};
    const session = await fetch(url+'/api/sessions',{method:'POST',headers});
    if (!session.ok) throw Error('Session request failed: '+session.status);
    const {id} = await session.json();
    output.clear(); output.show(true);
    await vscode.window.withProgress({location:vscode.ProgressLocation.Notification,title:'MyClaude',cancellable:true},async (_, token) => {
      const controller = new AbortController(); const sub = token.onCancellationRequested(()=>controller.abort());
      try {
        const response = await fetch(url+'/api/chat',{method:'POST',headers,signal:controller.signal,body:JSON.stringify({session_id:id,message:question+(selection ? '\n\nSelected code:\n'+selection : ''),permission:'read-only'})});
        if (!response.ok) throw Error('Chat request failed: '+response.status);
        const reader = response.body.getReader(), decoder = new TextDecoder(); let pending='', ended=false;
        while (true) { const {done,value}=await reader.read(); if(done)break; pending+=decoder.decode(value,{stream:true}); let boundary;
          while((boundary=pending.indexOf('\n\n'))>=0) {const frame=pending.slice(0,boundary);pending=pending.slice(boundary+2);
            for(const line of frame.split('\n')) if(line.startsWith('data: ')) {const e=JSON.parse(line.slice(6));if(e.type==='delta')output.append(e.text);if(e.type==='error')throw Error(e.message);if(e.type==='run_end'){ended=true;output.appendLine('\n['+e.status+']');}}
          }
        } if(!ended)throw Error('Stream interrupted; inspect run history');
      } finally {sub.dispose();}
    });
  }
  for (const [name,fn] of [['configure',configure],['ask',ask]]) context.subscriptions.push(vscode.commands.registerCommand('myclaude.'+name,()=>fn().catch(e=>vscode.window.showErrorMessage(e.name==='AbortError'?'MyClaude stopped':e.message))));
}
module.exports={activate};
