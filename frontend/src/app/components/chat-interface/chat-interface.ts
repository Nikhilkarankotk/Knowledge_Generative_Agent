import { Component, inject, ElementRef, ViewChild, AfterViewChecked, ChangeDetectorRef, OnInit, Output, EventEmitter } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Api } from '../../services/api/api';
import { marked } from 'marked';

export interface Message {
  id?: number;
  content: string;
  htmlContent?: string;
  role: 'user' | 'assistant';
  timestamp: Date;
  isError?: boolean;
  rating?: number; // 1 for thumbs up, -1 for thumbs down
  detectedLanguage?: string;
  isTranslated?: boolean;
  copied?: boolean;
}

@Component({
  selector: 'app-chat-interface',
  imports: [CommonModule, FormsModule],
  templateUrl: './chat-interface.html',
  styleUrls: ['./chat-interface.css', './chat-copilot.css']
})
export class ChatInterface implements AfterViewChecked, OnInit {
  private api = inject(Api);
  private cdr = inject(ChangeDetectorRef);

  @Output() messageSent = new EventEmitter<void>();

  messages: Message[] = [];
  userInput = '';
  isLoading = false;
  ingestError = '';

  attachedDocuments: any[] = [];
  processingFiles: string[] = [];
  attachedCollapsed = false;

  // Editing State
  editingMessageId: number | null = null;
  editedContent: string = '';

  // Correction Modal State
  showCorrectionModal = false;
  activeCorrectionMessage: Message | null = null;
  correctionText = '';

  // Export State (per-message response export + knowledge ZIP export)
  exportingMessageId: number | null = null;
  exportedMessageId: number | null = null;
  knowledgeExporting = false;
  knowledgeExported = false;

  @ViewChild('chatContainer') private chatContainer!: ElementRef;
  @ViewChild('fileInput') private fileInput!: ElementRef;

  ngOnInit() {
    this.loadHistory();
    this.loadAttachedDocuments();
  }

  loadChat(sessionId: string) {
    this.api.setSessionId(sessionId);
    this.attachedCollapsed = false;
    this.loadHistory();
    this.loadAttachedDocuments();
  }

  loadAttachedDocuments() {
    this.api.getAttachedDocuments().subscribe({
      next: (docs: any[]) => {
        this.attachedDocuments = docs ?? [];
        this.processingFiles = [];
        this.cdr.detectChanges();
      },
      error: () => {
        this.attachedDocuments = [];
        this.cdr.detectChanges();
      }
    });
  }

  loadHistory() {
    this.isLoading = true;
    this.api.getChatHistory().subscribe({
      next: async (history: any[]) => {
        this.messages = [];
        for (const msg of history ?? []) {
          const parsedContent = msg.content || '';
          const htmlParsed = msg.role === 'assistant' ? await marked.parse(parsedContent) : undefined;

          this.messages.push({
            id: msg.id,
            content: parsedContent,
            htmlContent: htmlParsed,
            role: msg.role as 'user' | 'assistant',
            timestamp: msg.timestamp ? new Date(msg.timestamp) : new Date(),
            detectedLanguage: msg.detectedLanguage,
            isTranslated: msg.isTranslated
          });
        }
        this.attachedCollapsed = this.messages.length > 0;
        this.isLoading = false;
        this.cdr.detectChanges();
        setTimeout(() => this.scrollToBottom(), 100);
      },
      error: () => {
        this.isLoading = false;
        console.error('Failed to load chat history');
        this.cdr.detectChanges();
      }
    });
  }

  ngAfterViewChecked() {
    this.scrollToBottom();
  }

  sendMessage() {
    if (!this.userInput.trim() || this.isLoading) return;

    const text = this.userInput.trim();
    this.messages.push({ content: text, role: 'user', timestamp: new Date() });
    if (this.messages.length === 1) {
      this.attachedCollapsed = true;
    }
    this.userInput = '';
    this.isLoading = true;

    this.api.sendChatMessage(text).subscribe({
      next: async (res: any) => {
        this.isLoading = false;
        try {
          const messageText = res.content || JSON.stringify(res);
          const htmlParsed = await marked.parse(messageText);

          this.messages.push({
            id: res.id,
            content: messageText,
            htmlContent: htmlParsed,
            role: 'assistant',
            timestamp: new Date(res.timestamp || new Date()),
            detectedLanguage: res.detectedLanguage,
            isTranslated: res.isTranslated
          });
          this.messageSent.emit();
        } catch (err: any) {
          console.error('Error parsing response in UI:', err);
          this.messages.push({
            content: `UI Parsing Error: ${err.message}`,
            role: 'assistant',
            isError: true,
            timestamp: new Date()
          });
        }
        this.cdr.detectChanges();
      },
      error: (err: any) => {
        this.isLoading = false;
        const errMessage = err.message || JSON.stringify(err);
        this.messages.push({
          content: `Backend Error: ${errMessage}`,
          role: 'assistant',
          isError: true,
          timestamp: new Date()
        });
        this.cdr.detectChanges();
      }
    });
  }

  // --- Export ---
  exportMessage(msg: Message) {
    const messageId = msg.id;
    if (!messageId || this.exportingMessageId !== null) return;
    this.exportingMessageId = messageId;
    this.api.downloadResponseExport(messageId).subscribe({
      next: (res) => {
        this.exportingMessageId = null;
        this.exportedMessageId = messageId;
        this.saveBlobFromResponse(res, `response-${messageId}.txt`);
        this.cdr.detectChanges();
      },
      error: () => {
        this.exportingMessageId = null;
        this.cdr.detectChanges();
      }
    });
  }

  exportKnowledge() {
    if (this.knowledgeExporting) return;
    const lastAssistant = [...this.messages].reverse().find((m) => m.role === 'assistant' && m.id);
    const messageId = lastAssistant?.id;
    if (!messageId) return;
    this.knowledgeExporting = true;
    this.api.downloadKnowledgeExport(messageId).subscribe({
      next: (res) => {
        this.knowledgeExporting = false;
        this.knowledgeExported = true;
        this.saveBlobFromResponse(res, `knowledge-export-${messageId}.zip`);
        this.cdr.detectChanges();
        setTimeout(() => (this.knowledgeExported = false), 3000);
      },
      error: () => {
        this.knowledgeExporting = false;
        this.cdr.detectChanges();
      }
    });
  }

  private saveBlobFromResponse(res: any, fallbackName: string) {
    const body = res.body as Blob | null;
    if (!body) return;
    const downloadUrl = URL.createObjectURL(body);
    const link = document.createElement('a');
    link.href = downloadUrl;
    const disposition = (res.headers?.get('Content-Disposition') as string | undefined) || '';
    const match = disposition.match(/filename="?([^";]+)"?/);
    link.download = match ? match[1] : fallbackName;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(() => URL.revokeObjectURL(downloadUrl), 10000);
  }

  // --- Action Methods ---
  copyToClipboard(msg: Message) {
    navigator.clipboard.writeText(msg.content).then(() => {
      msg.copied = true;
      setTimeout(() => msg.copied = false, 2000);
    });
  }

  startEditing(msg: Message) {
    if (msg.id) {
      this.editingMessageId = msg.id;
      this.editedContent = msg.content;
    }
  }

  cancelEditing() {
    this.editingMessageId = null;
    this.editedContent = '';
  }

  saveEdit(msg: Message) {
    if (!this.editedContent.trim()) return;

    const newText = this.editedContent.trim();
    msg.content = newText;
    msg.htmlContent = undefined; // Clear HTML so it re-renders if needed

    this.editingMessageId = null;
    this.userInput = newText;
    this.sendMessage(); // Resend the edited prompt
  }

  editPrompt(content: string) {
    this.userInput = content;
  }

  // --- Feedback & Correction ---
  rateMessage(msg: Message, rating: number) {
    if (!msg.id) return;
    const newRating = msg.rating === rating ? 0 : rating;
    this.api.submitFeedback(msg.id, newRating).subscribe({
      next: () => msg.rating = newRating,
      error: (err) => console.error('Feedback failed', err)
    });
  }

  openCorrectionModal(msg: Message) {
    this.activeCorrectionMessage = msg;
    this.correctionText = msg.content;
    this.showCorrectionModal = true;
  }

  closeCorrectionModal() {
    this.showCorrectionModal = false;
    this.activeCorrectionMessage = null;
    this.correctionText = '';
  }

  submitCorrection() {
    if (!this.activeCorrectionMessage?.id || !this.correctionText.trim()) return;

    this.api.submitFeedback(
      this.activeCorrectionMessage.id,
      this.activeCorrectionMessage.rating || 0,
      this.correctionText
    ).subscribe({
      next: () => {
        this.closeCorrectionModal();
        alert('Thank you! Your correction has been recorded.');
      },
      error: (err) => {
        console.error('Correction failed', err);
        alert('Failed to submit correction.');
      }
    });
  }

  formatTime(date: Date): string {
    return new Date(date).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
  }

  scrollToBottom(): void {
    if (this.chatContainer) {
      this.chatContainer.nativeElement.scrollTop = this.chatContainer.nativeElement.scrollHeight;
    }
  }

  onFileSelected(event: Event) {
    const input = event.target as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    if (files.length === 0) return;
    this.ingestDocuments(files);
    input.value = '';
  }

  openFilePicker() {
    if (this.fileInput) this.fileInput.nativeElement.click();
  }

  ingestDocuments(files: File[]) {
    this.ingestError = '';
    this.processingFiles = files.map((f) => f.name);
    this.cdr.detectChanges();

    const queue = [...files];
    const next = () => {
      const file = queue.shift();
      if (!file) {
        this.loadAttachedDocuments();
        return;
      }
      this.api.ingestRagDocument(file).subscribe({
        next: () => next(),
        error: () => {
          this.ingestError = `Failed to process ${file.name}. Please check the file format.`;
          this.loadAttachedDocuments();
        }
      });
    };
    next();
  }

  removeAttachedDocument(documentId: number) {
    this.api.deleteAttachedDocument(documentId).subscribe({
      next: () => this.loadAttachedDocuments(),
      error: () => {
        this.ingestError = 'Failed to remove document.';
      }
    });
  }

  clearChat() {
    this.messages = [];
    this.attachedDocuments = [];
    this.processingFiles = [];
    this.ingestError = '';
    this.attachedCollapsed = false;
    this.exportedMessageId = null;
    this.knowledgeExported = false;
    const newId = 'session_' + Math.random().toString(36).substring(2, 15);
    this.api.setSessionId(newId);
    this.loadAttachedDocuments();
  }
}