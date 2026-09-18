import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpHeaders, HttpResponse } from '@angular/common/http';
import { Observable } from 'rxjs';

@Injectable({
  providedIn: 'root',
})
export class Api {
  private http = inject(HttpClient);
  private backendUrl = 'http://127.0.0.1:8000/api';

  private activeSessionId: string | null = null;

  public setSessionId(id: string) {
    this.activeSessionId = id;
    localStorage.setItem('chatbot_session_id', id);
  }

  public getSessionId(): string {
    if (this.activeSessionId) return this.activeSessionId;
    let sessionId = localStorage.getItem('chatbot_session_id');
    if (!sessionId) {
      sessionId = 'session_' + Math.random().toString(36).substring(2, 15);
      localStorage.setItem('chatbot_session_id', sessionId);
    }
    this.activeSessionId = sessionId;
    return sessionId;
  }

  private getChatHeaders(isMultipart: boolean = false): HttpHeaders {
    let headers = new HttpHeaders({
      'X-Session-ID': this.getSessionId(),
      'Accept': '*/*'
    });
    
    if (!isMultipart) {
      headers = headers.set('Content-Type', 'application/json');
    }
    
    return headers;
  }

  // --- Chat API ---
  sendChatMessage(message: string): Observable<any> {
    return this.http.post(`${this.backendUrl}/chat`, { message }, { headers: this.getChatHeaders(false) });
  }

  sendChatFile(message: string, file: File): Observable<any> {
    const formData = new FormData();
    formData.append('message', message);
    formData.append('pdf', file);
    return this.http.post(`${this.backendUrl}/chat`, formData, { headers: this.getChatHeaders(true) });
  }

  getChatHistory(): Observable<any[]> {
    return this.http.get<any[]>(`${this.backendUrl}/history`, { headers: this.getChatHeaders(false) });
  }

  // --- Session History API ---
  getChatSessions(): Observable<any[]> {
    return this.http.get<any[]>(`${this.backendUrl}/history/sessions`, { headers: this.getChatHeaders(false) });
  }

  deleteChatSession(sessionId: string): Observable<any> {
    return this.http.delete(`${this.backendUrl}/history/sessions/${sessionId}`);
  }

  submitFeedback(messageId: number, rating: number, correctedAnswer?: string): Observable<any> {
    const body = { messageId, rating, correctedAnswer };
    return this.http.post(`${this.backendUrl}/feedback`, body, { 
      headers: this.getChatHeaders(false) 
    });
  }

  // --- Mistral API ---
  uploadMistralFileAndMessage(file: File, message: string): Observable<any> {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('message', message);
    return this.http.post(`${this.backendUrl}/mistral/upload`, formData, { responseType: 'text' as 'json' });
  }

  // --- RAG API ---
  ingestRagDocument(file: File): Observable<any> {
    const formData = new FormData();
    formData.append('file', file);
    return this.http.post(`${this.backendUrl}/rag/ingest`, formData, { 
      headers: this.getChatHeaders(true),
      responseType: 'text' as 'json' 
    });
  }

  getAttachedDocuments(): Observable<any[]> {
    return this.http.get<any[]>(`${this.backendUrl}/rag/documents`, {
      headers: this.getChatHeaders(false)
    });
  }

  deleteAttachedDocument(documentId: number): Observable<any> {
    return this.http.delete(`${this.backendUrl}/rag/documents/${documentId}`, {
      headers: this.getChatHeaders(false),
      responseType: 'text' as 'json'
    });
  }

  queryRag(query: string): Observable<any> {
    return this.http.post(`${this.backendUrl}/rag/query`, query, {
      headers: this.getChatHeaders(false),
      responseType: 'text' as 'json'
    });
  }

  // --- Dashboard API ---
  getTopExperts(chatMessageId?: number): Observable<any> {
    const params: Record<string, number> = {};
    if (chatMessageId !== undefined && chatMessageId !== null) {
      params['chatMessageId'] = chatMessageId;
    }
    return this.http.get<any>(`${this.backendUrl}/experts`, {
      headers: this.getChatHeaders(false),
      params
    });
  }

  // --- Export API ---
  getKnowledgeExportMetadata(chatMessageId: number): Observable<any> {
    return this.http.get<any>(`${this.backendUrl}/knowledge-export/${chatMessageId}`, {
      headers: this.getChatHeaders(false)
    });
  }

  downloadResponseExport(chatMessageId: number): Observable<HttpResponse<Blob>> {
    return this.http.post(`${this.backendUrl}/export/${chatMessageId}`, {}, {
      headers: this.getChatHeaders(false),
      responseType: 'blob',
      observe: 'response'
    });
  }

  downloadKnowledgeExport(chatMessageId: number, hint?: string): Observable<HttpResponse<Blob>> {
    return this.http.post(`${this.backendUrl}/knowledge-export/${chatMessageId}`, { hint }, {
      headers: this.getChatHeaders(false),
      responseType: 'blob',
      observe: 'response'
    });
  }

  /**
   * Download ONLY the Confluence documents used to generate one AI response
   * (`confluence-sources.zip`: `Confluence/<Page>.docx` per page + `metadata.json`).
   * No chat history, no earlier queries, no other responses' sources.
   */
  downloadResponseSources(chatMessageId: number): Observable<HttpResponse<Blob>> {
    return this.http.post(`${this.backendUrl}/knowledge-export/${chatMessageId}/sources`, {}, {
      headers: this.getChatHeaders(false),
      responseType: 'blob',
      observe: 'response'
    });
  }

  /**
   * Download the Confluence documents behind the most recent response in the
   * current session that actually used Confluence pages. The server resolves the
   * target message, so this works after a page refresh regardless of which
   * message happens to be last.
   */
  downloadLatestResponseSources(): Observable<HttpResponse<Blob>> {
    return this.http.post(`${this.backendUrl}/knowledge-export/latest/sources`, {}, {
      headers: this.getChatHeaders(false),
      responseType: 'blob',
      observe: 'response'
    });
  }

  /**
   * Download the whole chat session as one organized `Export.zip`:
   * `ChatHistory/QueryN_Response.docx`, `Confluence/QueryN/<Page>.docx` (only the
   * pages used for that answer, each written once) and a `metadata.json`
   * query -> source mapping. Scoped by the X-Session-ID header.
   */
  downloadSessionExport(): Observable<HttpResponse<Blob>> {
    return this.http.post(`${this.backendUrl}/knowledge-export/session`, {}, {
      headers: this.getChatHeaders(false),
      responseType: 'blob',
      observe: 'response'
    });
  }
}

