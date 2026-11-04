/* Isolated Windows picker through the authenticated local service. */
window.rptsNative = (() => {
 let pending=false;
 async function chooseFolder(initial='',kind='game') {
  if(pending)throw new Error('Окно выбора папки уже открыто.');
  pending=true;
  try {
   const result=await api('/api/pick-folder',{kind,initial:String(initial).slice(0,2048)});
   if(result.path===null||typeof result.path==='string')return result.path;
   throw new Error('Окно выбора вернуло неверный путь.');
  } finally { pending=false; }
 }
 return {chooseFolder};
})();
