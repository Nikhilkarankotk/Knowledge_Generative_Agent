import { Component, OnInit, inject, Output, EventEmitter, ViewChildren, QueryList, ElementRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Api } from '../../services/api/api';

@Component({
  selector: 'app-sidebar',
  imports: [CommonModule],
  templateUrl: './sidebar.html',
  styleUrl: './sidebar.css'
})
export class Sidebar implements OnInit {
  private api = inject(Api);

  @Output() newChat = new EventEmitter<void>();
  @Output() selectChat = new EventEmitter<string>();

  history: any[] = [];
  loadError = false;
  activeMenu: string | null = null;

  ngOnInit() {
    this.loadSessions();
  }

  loadSessions() {
    this.api.getChatSessions().subscribe({
      next: (data) => {
        this.history = data ?? [];
      },
      error: (err) => {
        this.loadError = true;
      }
    });
  }

  onNewChat() {
    this.newChat.emit();
  }

  onSelectChat(sessionId: string) {
    this.selectChat.emit(sessionId);
  }

  toggleMenu(sessionId: string) {
    this.activeMenu = this.activeMenu === sessionId ? null : sessionId;
  }

  getCustomTitle(sessionId: string): string | null {
    return localStorage.getItem('rename_' + sessionId);
  }

  renamingSessionId: string | null = null;
  @ViewChildren('nameInput') nameInputs!: QueryList<ElementRef>;

  startRename(sessionId: string) {
    this.renamingSessionId = sessionId;
    this.activeMenu = null;
    setTimeout(() => {
      if (this.nameInputs && this.nameInputs.first) {
        this.nameInputs.first.nativeElement.focus();
        this.nameInputs.first.nativeElement.select();
      }
    }, 0);
  }

  saveRename(sessionId: string, newTitle: string) {
    if (newTitle && newTitle.trim().length > 0) {
      localStorage.setItem('rename_' + sessionId, newTitle.trim());
    }
    this.renamingSessionId = null;
  }

  cancelRename() {
    this.renamingSessionId = null;
  }

  deleteChat(sessionId: string) {
    if (confirm('Are you sure you want to delete this chat session?')) {
      this.api.deleteChatSession(sessionId).subscribe({
        next: () => {
          localStorage.removeItem('rename_' + sessionId);
          // If the deleted chat is the current active session, trigger newChat
          if (this.api.getSessionId() === sessionId) {
             this.onNewChat();
          }
          this.loadSessions();
        },
        error: (err) => {
          console.error('Failed to delete chat:', err);
          alert('Failed to delete chat.');
        }
      });
    }
    this.activeMenu = null;
  }
}
